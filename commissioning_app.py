import pandas as pd
import streamlit as st
import altair as alt
import re

# Set Streamlit page config
st.set_page_config(page_title="MHE Staffing & Cost Model", layout="wide")


# ---------------------------------------------------
# CORE MODEL FUNCTIONS
# ---------------------------------------------------

def normalize_cols(df):
    """Standardizes column names: strips whitespace, replaces spaces with underscores."""
    if df is None: return None
    df.columns = [str(c).strip().replace(" ", "_") for c in df.columns]
    return df


def load_inputs_from_excel(excel_source):
    """
    Loads POR, Scenarios, KNOWN_DATES, CENTRAL_TEAM.
    Dynamically loads ANY sheet ending in '_Assumptions'.
    """
    known_dates = pd.DataFrame(columns=['BuildingType', 'Year', 'Project_ID', 'Go-Live Date'])
    central_team = pd.DataFrame()
    assumptions_dict = {}

    try:
        xl = pd.ExcelFile(excel_source)
    except Exception:
        return None, None, None, known_dates, central_team

    try:
        if "POR" not in xl.sheet_names or "Scenario_Params" not in xl.sheet_names:
            st.error("Missing required sheets: 'POR' or 'Scenario_Params'.")
            return None, None, None, known_dates, central_team

        por = pd.read_excel(xl, sheet_name="POR")
        scenarios = pd.read_excel(xl, sheet_name="Scenario_Params")

        if "KNOWN_DATES" in xl.sheet_names:
            known_dates = pd.read_excel(xl, sheet_name="KNOWN_DATES")
            if 'Start_Date' in known_dates.columns and 'Go-Live Date' not in known_dates.columns:
                known_dates = known_dates.rename(columns={'Start_Date': 'Go-Live Date'})
            known_dates['Go-Live Date'] = pd.to_datetime(known_dates['Go-Live Date'])
            if 'Year' not in known_dates.columns:
                known_dates['Year'] = known_dates['Go-Live Date'].dt.year
            if 'Project_ID' not in known_dates.columns:
                known_dates['Project_ID'] = known_dates['BuildingType'] + '-' + known_dates['Year'].astype(
                    str) + '-ID-' + (known_dates.index + 1).astype(str)

        if "CENTRAL_TEAM" in xl.sheet_names:
            central_team = pd.read_excel(xl, sheet_name="CENTRAL_TEAM")
            central_team = normalize_cols(central_team)
            if 'Category' not in central_team.columns:
                central_team['Category'] = "Fixed"

        for sheet in xl.sheet_names:
            if sheet.endswith("_Assumptions"):
                prefix = sheet.replace("_Assumptions", "")
                try:
                    df = pd.read_excel(xl, sheet_name=sheet)
                    df = normalize_cols(df)

                    # --- FIX: Case-Insensitive Column Check ---
                    # Check if any column starts with prefix_Staff (ignoring case)
                    target_start = f"{prefix}_Staff".lower()
                    col_match = any(col.lower().startswith(target_start) for col in df.columns)

                    if col_match:
                        # Normalize Column Names to match Prefix case if needed?
                        # Actually, we just need to identify the sheet.
                        # We will store it with the UPPERCASE prefix to match logic elsewhere if possible,
                        # or just keep the prefix as is and handle mapping downstream.

                        # Default Category if missing
                        if 'Category' not in df.columns:
                            if "Install" in sheet or "INSTALL" in prefix.upper():
                                df['Category'] = "TEMP"
                            else:
                                df['Category'] = "VAR"
                        assumptions_dict[prefix] = df
                except Exception as e:
                    st.warning(f"Could not load {sheet}: {e}")

    except ValueError as e:
        st.error(f"Error parsing Excel file: {e}")
        raise e

    return por, scenarios, assumptions_dict, known_dates, central_team


def build_efficiency_map(assumptions_df: pd.DataFrame, years):
    eff = {}
    req_cols = ["BuildingType", "Baseline_Year", "Annual_Efficiency_Improvement"]
    if not all(col in assumptions_df.columns for col in req_cols):
        return {}

    for _, row in assumptions_df.iterrows():
        btype = row["BuildingType"]
        try:
            baseline_year = int(row["Baseline_Year"])
            annual_impr = float(row["Annual_Efficiency_Improvement"])
        except ValueError:
            continue

        for y in years:
            if y < baseline_year:
                factor = 1.0
            else:
                factor = (1.0 - annual_impr) ** (y - baseline_year)
            eff[(btype, y)] = factor
    return eff


def assign_go_live_months_quarter_based(count, year, q_shares):
    if count <= 0: return []
    quarter_months = {"Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    q_counts = {}
    remaining = count
    for i, q in enumerate(quarters):
        share = float(q_shares.get(q, 0))
        if i < 3:
            qc = int(round(count * share))
            q_counts[q] = qc
            remaining -= qc
        else:
            q_counts[q] = max(0, remaining)
    diff = count - sum(q_counts.values())
    q_counts["Q4"] += diff

    months = []
    for q in quarters:
        q_count = q_counts[q]
        if q_count <= 0: continue
        start_m, end_m = quarter_months[q]
        if q_count > 0:
            step = (end_m - start_m + 1) / q_count
            for k in range(q_count):
                target_month = start_m + int(k * step)
                if target_month > end_m: target_month = end_m
                months.append((year, target_month))
    return months


def build_monthly_labor_detailed(por: pd.DataFrame,
                                 assumptions_df: pd.DataFrame,
                                 scenarios: pd.DataFrame,
                                 prefix: str,
                                 known_dates: pd.DataFrame,
                                 fallback_scen_name: str,
                                 selected_scen_name: str):
    year_map = {}
    for c in por.columns:
        if str(c).isdigit(): year_map[int(c)] = c
    years = sorted(year_map.keys())

    if not years or assumptions_df.empty:
        return {}

    eff_map = build_efficiency_map(assumptions_df, years)
    assumptions_idx = assumptions_df.set_index("BuildingType")

    # --- FIX: ROBUST COLUMN FINDER ---
    # Find the specific columns regardless of case
    def get_col_case_insensitive(df, target):
        for col in df.columns:
            if col.lower() == target.lower():
                return col
        return None

    staff_col = get_col_case_insensitive(assumptions_idx, f"{prefix}_Staff_Per_Launch")
    dur_col = get_col_case_insensitive(assumptions_idx, f"{prefix}_Duration_Months")
    lead_col = get_col_case_insensitive(assumptions_idx, f"{prefix}_Lead_Months")
    rate_col = get_col_case_insensitive(assumptions_idx, "Annual_Rate")
    cat_col = get_col_case_insensitive(assumptions_idx, "Category")

    if not staff_col:
        return pd.DataFrame()

    scen_to_use = selected_scen_name if selected_scen_name != 'HYBRID' else fallback_scen_name
    if scen_to_use not in scenarios['ScenarioName'].values:
        scen_to_use = scenarios['ScenarioName'].iloc[0]

    scenario_row = scenarios[scenarios['ScenarioName'] == scen_to_use].iloc[0]
    q_shares = {k: scenario_row.get(f"{k}_Share", 0.0) for k in ["Q1", "Q2", "Q3", "Q4"]}

    rows = []
    known_dates_map = {}
    if not known_dates.empty:
        for _, kd_row in known_dates.iterrows():
            key = (kd_row['BuildingType'], kd_row['Year'])
            if key not in known_dates_map: known_dates_map[key] = []
            known_dates_map[key].append((kd_row['Project_ID'], kd_row['Go-Live Date']))

    for _, row in por.iterrows():
        btype = row["BuildingType"]
        if btype not in assumptions_idx.index: continue

        try:
            staff_per_launch = float(assumptions_idx.loc[btype, staff_col])
            duration = int(assumptions_idx.loc[btype, dur_col])
            lead_time = int(assumptions_idx.loc[btype, lead_col])

            annual_rate = 0
            if rate_col:
                val = assumptions_idx.loc[btype, rate_col]
                annual_rate = float(val) if pd.notnull(val) else 0

            category = "VAR"
            if cat_col:
                val = assumptions_idx.loc[btype, cat_col]
                category = str(val) if pd.notnull(val) else "VAR"

        except (ValueError, KeyError):
            continue

        if staff_per_launch == 0: continue

        for y in years:
            actual_col = year_map[y]
            launches = row[actual_col]
            if pd.isna(launches) or launches == 0: continue
            launches = int(launches)

            go_live_months = assign_go_live_months_quarter_based(launches, int(y), q_shares)
            known_launches = known_dates_map.get((btype, int(y)), [])
            num_known = len(known_launches)

            project_details = []
            for i in range(num_known):
                project_details.append({'Go_Live_Date': known_launches[i][1], 'Project_ID': known_launches[i][0]})
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue
                gy, gm = go_live_months[i]
                go_live_date = pd.Timestamp(year=gy, month=gm, day=1)
                project_details.append({'Go_Live_Date': go_live_date, 'Project_ID': f"{btype}-{y}-{i + 1}"})

            for detail in project_details:
                go_live_date = detail['Go_Live_Date']
                project_id = detail['Project_ID']
                start_date = go_live_date - pd.DateOffset(months=lead_time)
                end_date = start_date + pd.DateOffset(months=duration - 1)

                proj_months = pd.date_range(start=start_date, end=end_date, freq="MS")

                for dt in proj_months:
                    yr = dt.year
                    eff = eff_map.get((btype, yr), 1.0)
                    fte = staff_per_launch * eff
                    monthly_cost = fte * (annual_rate / 12.0)

                    rows.append({
                        "Scenario": selected_scen_name,
                        "Month": dt,
                        "BuildingType": btype,
                        "FTE": fte,
                        "Cost": monthly_cost,
                        "Project_ID": project_id,
                        "Category": category
                    })

    if rows:
        df = pd.DataFrame(rows)
        return df
    else:
        return pd.DataFrame(columns=["Month", "BuildingType", "Project_ID", "FTE", "Cost", "Scenario", "Category"])


def build_central_team_demand(central_df):
    rows = []
    if central_df.empty: return pd.DataFrame()

    year_map = {}
    for c in central_df.columns:
        if str(c).isdigit(): year_map[int(c)] = c
    years = sorted(year_map.keys())

    # Case-insensitive column finding
    def get_col(df, name):
        for c in df.columns:
            if c.lower() == name.lower(): return c
        return None

    col_team = get_col(central_df, 'Team')
    col_role = get_col(central_df, 'Role')
    col_rate = get_col(central_df, 'Annual_Rate')
    col_cat = get_col(central_df, 'Category')
    col_fte = get_col(central_df, 'FTE_Count')

    # Basic Validation
    if not (col_team and col_role and col_rate): return pd.DataFrame()

    if years:
        start_year, end_year = min(years), max(years)
    else:
        start_year, end_year = 2025, 2035

    for _, row in central_df.iterrows():
        team = row[col_team]
        role = row[col_role]
        rate = row[col_rate] if pd.notnull(row[col_rate]) else 0
        cat = row[col_cat] if col_cat and pd.notnull(row[col_cat]) else "Fixed"

        for y in range(start_year, end_year + 1):
            if y in year_map:
                fte = row.get(year_map[y], 0)
            elif col_fte:
                fte = row.get(col_fte, 0)
            else:
                fte = 0

            if pd.isna(fte) or fte == 0: continue

            monthly_cost = fte * (rate / 12.0)
            for m in range(1, 13):
                dt = pd.Timestamp(year=y, month=m, day=1)
                rows.append({
                    "Month": dt,
                    "BuildingType": team,
                    "Project_ID": "Static_Central",
                    "FTE": fte,
                    "Cost": monthly_cost,
                    "Role_Override": role,
                    "Category": cat
                })

    return pd.DataFrame(rows)


def get_team_assignment(role, btype):
    # Case-Insensitive Role matching
    r_lower = role.lower()
    if "ce" in r_lower or "commissioning" in r_lower: return "System Integration"

    s = str(btype).upper()
    if "ARS" in s: return "ARS Team"
    if "SSD" in s: return "SSD Team"
    if "IBIS" in s or "AUTOSTORE" in s: return "Projects Team"
    if "Central" in btype or btype in ["ARS Team", "SSD Team", "System Integration", "Projects Team"]: return btype
    return "Other"


def filter_results_by_scope(results, selected_teams, selected_years, selected_roles):
    filtered = {}
    for label, df in results.items():
        if label not in selected_roles and not any(label.startswith(r) for r in selected_roles):
            if label not in selected_roles: continue

        if df is None or df.empty:
            filtered[label] = df
            continue

        df_work = df.copy()
        if "Role_Override" in df_work.columns:
            df_work["Team"] = df_work["BuildingType"]
        else:
            df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))

        df_work["Year"] = df_work["Month"].dt.year

        mask = (df_work["Team"].isin(selected_teams)) & (df_work["Year"].isin(selected_years))
        filtered_df = df_work[mask]

        if not filtered_df.empty: filtered[label] = filtered_df
    return filtered


def build_split_matrices(results, baseline_quantile, metric_col):
    summary_rows = []
    detail_team_rows = []

    known_map = {
        "CE": "Commissioning Engineer", "ME": "Mechanical Engineer",
        "EE": "Electrical Engineer", "SITEOPS": "Site Operations",
        "LEAD": "Site Lead", "INSTALL": "Installation", "Install": "Installation"
    }

    for label, df in results.items():
        if df is None or df.empty: continue

        is_central = "Role_Override" in df.columns

        df_work = df.copy()
        if "Team" not in df_work.columns:
            if is_central:
                df_work["Team"] = df_work["BuildingType"]
            else:
                df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))

        df_work["Year"] = df_work["Month"].dt.year

        cols_to_group = ["Year", "Month", "Team", "Category"]
        if is_central:
            cols_to_group.append("Role_Override")
        else:
            cols_to_group.append("BuildingType")

        monthly_sum = df_work.groupby(cols_to_group, as_index=False)[metric_col].sum()

        if is_central:
            agg_func = 'sum' if metric_col == "Cost" else 'max'
            grouped = df_work.groupby(["Year", "Team", "Role_Override", "Category"], as_index=False)[metric_col].agg(
                agg_func)

            for _, row in grouped.iterrows():
                summary_rows.append({"Category": row['Category'], "Year": row['Year'], "Value": row[metric_col]})
                detail_team_rows.append(
                    {"Team": row['Team'], "Role": row['Role_Override'], "Year": row['Year'], "Value": row[metric_col]})

        else:
            display_role = known_map.get(label, label)
            cols_annual = ["Year", "Team", "Category"]
            m_grp = df_work.groupby(["Year", "Month", "Team", "Category"], as_index=False)[metric_col].sum()

            if metric_col == "Cost":
                a_grp = m_grp.groupby(cols_annual, as_index=False)[metric_col].sum()
            else:
                a_grp = m_grp.groupby(cols_annual, as_index=False)[metric_col].max()

            for _, row in a_grp.iterrows():
                val = row[metric_col]
                cat_input = row['Category']

                if cat_input == "TEMP":
                    int_val, con_val = 0, val
                else:
                    int_val = val * baseline_quantile
                    con_val = val - int_val

                if int_val > 0.01:
                    summary_rows.append({"Category": cat_input, "Year": row['Year'], "Value": int_val})
                    detail_team_rows.append(
                        {"Team": row['Team'], "Role": display_role, "Year": row['Year'], "Value": int_val})
                if con_val > 0.01:
                    summary_rows.append({"Category": "TEMP", "Year": row['Year'], "Value": con_val})
                    c_name = display_role if cat_input == "TEMP" else f"{display_role} (Contractor)"
                    detail_team_rows.append(
                        {"Team": row['Team'], "Role": c_name, "Year": row['Year'], "Value": con_val})

    if summary_rows:
        sum_df = pd.DataFrame(summary_rows)
        sum_matrix = sum_df.pivot_table(index="Category", columns="Year", values="Value", aggfunc="sum").fillna(0)
        # Sort Order
        desired_order = ["Fixed", "VAR", "TEMP", "Total Internal", "Total Contractors"]
        existing_order = [x for x in desired_order if x in sum_matrix.index]
        remaining = [x for x in sum_matrix.index if x not in desired_order]
        sum_matrix = sum_matrix.reindex(existing_order + remaining)

        sum_matrix.loc["Grand Total"] = sum_matrix.sum()
    else:
        sum_matrix = pd.DataFrame()

    if detail_team_rows:
        det_df = pd.DataFrame(detail_team_rows)
        det_matrix = det_df.pivot_table(index=["Team", "Role"], columns="Year", values="Value", aggfunc="sum").fillna(0)
    else:
        det_matrix = pd.DataFrame()

    return sum_matrix, det_matrix


def build_building_type_matrix(results, baseline_quantile, metric_col):
    rows = []
    known_map = {"CE": "Commissioning Engineer", "ME": "Mechanical Engineer", "EE": "Electrical Engineer",
                 "SITEOPS": "Site Operations", "LEAD": "Site Lead", "INSTALL": "Installation",
                 "Install": "Installation"}

    for label, df in results.items():
        if df is None or df.empty: continue
        if "Role_Override" in df.columns: continue

        display_role = known_map.get(label, label)
        df_work = df.copy()
        if "Year" not in df_work.columns: df_work["Year"] = df_work["Month"].dt.year

        m_grp = df_work.groupby(["Year", "Month", "BuildingType", "Category"], as_index=False)[metric_col].sum()
        if metric_col == "Cost":
            a_grp = m_grp.groupby(["Year", "BuildingType", "Category"], as_index=False)[metric_col].sum()
        else:
            a_grp = m_grp.groupby(["Year", "BuildingType", "Category"], as_index=False)[metric_col].max()

        for _, row in a_grp.iterrows():
            val = row[metric_col]
            cat_input = row['Category']

            if cat_input == "TEMP":
                i_v, c_v = 0, val
            else:
                i_v = val * baseline_quantile
                c_v = val - i_v

            if i_v > 0.01: rows.append(
                {"Building Type": row['BuildingType'], "Role": display_role, "Year": row['Year'], "Value": i_v})
            if c_v > 0.01: rows.append(
                {"Building Type": row['BuildingType'], "Role": f"{display_role} (Contractor)", "Year": row['Year'],
                 "Value": c_v})

    if not rows: return pd.DataFrame()
    return pd.DataFrame(rows).pivot_table(index=["Building Type", "Role"], columns="Year", values="Value",
                                          aggfunc="sum").fillna(0)


def build_quarterly_ramp(results, metric_col):
    rows = []
    known_map = {"CE": "Commissioning Engineer", "ME": "Mechanical Engineer", "EE": "Electrical Engineer",
                 "SITEOPS": "Site Operations", "LEAD": "Site Lead", "INSTALL": "Installation",
                 "Install": "Installation"}

    for label, df in results.items():
        if df is None or df.empty: continue

        is_central = "Role_Override" in df.columns
        role_name = df['Role_Override'].iloc[0] if is_central else known_map.get(label, label)

        df_work = df.copy()
        df_work['Quarter'] = df_work['Month'].dt.to_period('Q').astype(str)
        if "Team" not in df_work.columns:
            if is_central:
                df_work["Team"] = df_work["BuildingType"]
            else:
                df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))

        m_curve = df_work.groupby(['Quarter', 'Month', 'Team'], as_index=False)[metric_col].sum()
        if metric_col == "Cost":
            q_val = m_curve.groupby(['Quarter', 'Team'], as_index=False)[metric_col].sum()
        else:
            q_val = m_curve.groupby(['Quarter', 'Team'], as_index=False)[metric_col].max()

        for _, row in q_val.iterrows():
            rows.append({"Team": row['Team'], "Role": role_name, "Quarter": row['Quarter'], "Value": row[metric_col]})

    if not rows: return pd.DataFrame()
    matrix = pd.DataFrame(rows).pivot_table(index=["Team", "Role"], columns="Quarter", values="Value",
                                            aggfunc="sum").fillna(0)
    return matrix[sorted(matrix.columns)]


def build_team_monthly_data(results, metric_col):
    all_dfs = []
    for label, df in results.items():
        if df is None or df.empty: continue
        temp = df.copy()
        if "Team" not in temp.columns:
            if "Role_Override" in temp.columns:
                temp["Team"] = temp["BuildingType"]
            else:
                temp["Team"] = temp["BuildingType"].apply(lambda b: get_team_assignment(label, b))
        all_dfs.append(temp[["Month", "Team", metric_col]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team"], as_index=False)[metric_col].sum()


def build_resource_breakdown_data(results, metric_col):
    all_dfs = []
    known_map = {"CE": "Commissioning Engineer", "ME": "Mechanical Engineer", "EE": "Electrical Engineer",
                 "SITEOPS": "Site Operations", "LEAD": "Site Lead", "INSTALL": "Installation",
                 "Install": "Installation"}

    for label, df in results.items():
        if df is None or df.empty: continue
        temp = df.copy()
        if "Team" not in temp.columns:
            if "Role_Override" in temp.columns:
                temp["Team"] = temp["BuildingType"]
            else:
                temp["Team"] = temp["BuildingType"].apply(lambda b: get_team_assignment(label, b))

        if "Role_Override" in temp.columns:
            temp["Discipline"] = temp["Role_Override"]
        else:
            temp["Discipline"] = known_map.get(label, label)

        all_dfs.append(temp[["Month", "Team", "Discipline", metric_col]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team", "Discipline"], as_index=False)[
        metric_col].sum()


def build_project_level_matrix(results, baseline_quantile, view_mode, metric_col):
    dfs = []
    for label, df in results.items():
        if df is None or df.empty: continue
        if "Role_Override" in df.columns: continue

        temp = df.copy()
        if "Year" not in temp.columns: temp['Year'] = temp['Month'].dt.year

        val = temp[metric_col]
        cat_input = temp['Category'].iloc[0] if 'Category' in temp.columns else "VAR"

        if cat_input == "TEMP":
            temp['Internal'] = 0
            temp['Contractor'] = val
        else:
            temp['Internal'] = val * baseline_quantile
            temp['Contractor'] = val - temp['Internal']

        if view_mode == "Internal":
            temp['Value'] = temp['Internal']
        elif view_mode == "Contractor":
            temp['Value'] = temp['Contractor']
        else:
            temp['Value'] = val

        dfs.append(temp[['Year', 'BuildingType', 'Project_ID', 'Month', 'Value']])

    if not dfs: return pd.DataFrame()
    combined = pd.concat(dfs, ignore_index=True)
    m_sum = combined.groupby(['Year', 'BuildingType', 'Project_ID', 'Month'], as_index=False)['Value'].sum()
    if metric_col == "Cost":
        a_val = m_sum.groupby(['Year', 'BuildingType', 'Project_ID'], as_index=False)['Value'].sum()
    else:
        a_val = m_sum.groupby(['Year', 'BuildingType', 'Project_ID'], as_index=False)['Value'].max()
    return a_val.pivot_table(index=['BuildingType', 'Project_ID'], columns='Year', values='Value',
                             aggfunc='sum').fillna(0)


def build_project_master_list(por, known_dates, scenarios, fallback_scen_name, selected_teams, selected_years):
    master_list_rows = []
    year_map = {}
    for c in por.columns:
        if str(c).isdigit(): year_map[int(c)] = c
    years = sorted(year_map.keys())

    if fallback_scen_name not in scenarios['ScenarioName'].values:
        fallback_scen_name = scenarios['ScenarioName'].iloc[0]

    fallback_row = scenarios[scenarios['ScenarioName'] == fallback_scen_name].iloc[0]
    q_shares = {k: fallback_row.get(f"{k}_Share", 0.0) for k in ["Q1", "Q2", "Q3", "Q4"]}

    known_starts_map = {}
    if not known_dates.empty:
        for _, kd_row in known_dates.iterrows():
            key = (kd_row['BuildingType'], kd_row['Year'])
            if key not in known_starts_map: known_starts_map[key] = []
            known_starts_map[key].append((kd_row['Project_ID'], kd_row['Go-Live Date']))

    for _, row in por.iterrows():
        btype = row["BuildingType"]
        team_name = get_team_assignment("Generic", btype)
        if team_name not in selected_teams: continue

        for y in years:
            if y not in selected_years: continue
            actual_col = year_map[y]
            launches = row[actual_col]
            if pd.isna(launches) or launches == 0: continue
            launches = int(launches)

            known_launches = known_starts_map.get((btype, y), [])
            num_known = len(known_launches)
            go_live_months = assign_go_live_months_quarter_based(launches, y, q_shares)

            for i in range(num_known):
                master_list_rows.append({'Building Type': btype, 'Project ID': known_launches[i][0],
                                         'Go-Live Date': known_launches[i][1].strftime('%Y-%m-%d'),
                                         'Source': 'Known Date'})
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue
                gy, gm = go_live_months[i]
                scheduled = pd.Timestamp(year=gy, month=gm, day=1)
                master_list_rows.append({'Building Type': btype, 'Project ID': f"{btype}-{y}-{i + 1}",
                                         'Go-Live Date': scheduled.strftime('%Y-%m-%d'),
                                         'Source': f'Fallback ({fallback_scen_name})'})

    final_df = pd.DataFrame(master_list_rows)
    if final_df.empty: return pd.DataFrame(columns=['Building Type', 'Project ID', 'Go-Live Date', 'Source'])
    return final_df[['Building Type', 'Project ID', 'Go-Live Date', 'Source']].sort_values('Go-Live Date')


# ---------------------------------------------------
# STREAMLIT APP MAIN FUNCTION
# ---------------------------------------------------

def main():
    st.title("MHE Integration Staffing & Cost Model")

    # 1. LOAD DATA
    st.sidebar.header("1. Input Data Source")
    uploaded_file = st.sidebar.file_uploader("Upload commissioning_input.xlsx", type=["xlsx"])
    if uploaded_file is None:
        st.warning("Please upload your input Excel file to begin.")
        return

    por, scenarios, assumptions_dict, known_dates, central_team = load_inputs_from_excel(uploaded_file)
    if por is None: return

    # 2. CONFIG
    st.sidebar.header("2. Configuration")
    raw_options = scenarios["ScenarioName"].dropna().unique()
    scenario_options = [x for x in raw_options if str(x).strip() != ""]
    if not known_dates.empty: scenario_options.insert(0, "HYBRID")

    selected_scen = st.sidebar.selectbox("Select Scenario to Analyze", options=scenario_options)

    fallback_choice = None
    if selected_scen == 'HYBRID':
        st.sidebar.markdown('**Hybrid Fallback:**')
        fallback_options = [x for x in scenarios["ScenarioName"].dropna().unique() if str(x).strip() != ""]
        default_index = 0
        if "LEVEL_LOAD" in fallback_options: default_index = list(fallback_options).index("LEVEL_LOAD")
        fallback_choice = st.sidebar.selectbox("Select Fallback Schedule", options=fallback_options,
                                               index=default_index)
    else:
        fallback_choice = selected_scen

    if selected_scen != 'HYBRID':
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == selected_scen].copy()
    else:
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == fallback_choice].copy()

    # 3. STRATEGY
    st.sidebar.header("3. Internal Staffing Strategy")
    baseline_choice = st.sidebar.selectbox("Baseline Headcount Rule",
                                           ["Lean (P50)", "Moderate (P70)", "Robust (P90)", "Max (P100)"], index=3)
    b_map = {"Lean (P50)": 0.5, "Moderate (P70)": 0.7, "Robust (P90)": 0.9, "Max (P100)": 1.0}
    baseline_quantile = b_map[baseline_choice]

    # 4. VIEW TOGGLE (HEADCOUNT vs COST)
    st.markdown("### 📊 View Mode")
    metric_choice = st.radio("Select Metric:", ["Headcount (FTE)", "Cost ($)"], horizontal=True)
    metric_col = "Cost" if metric_choice == "Cost ($)" else "FTE"
    fmt = "${:,.0f}" if metric_col == "Cost" else "{:.1f}"

    # 5. GENERATE RAW DATA (Variable)
    raw_results = {}
    for prefix, df_assump in assumptions_dict.items():
        raw_results[prefix] = build_monthly_labor_detailed(por, df_assump, filtered_scenarios, prefix, known_dates,
                                                           fallback_choice, selected_scen)

    # 6. GENERATE RAW DATA (Central)
    if not central_team.empty:
        central_monthly = build_central_team_demand(central_team)
        if not central_monthly.empty:
            for role, group in central_monthly.groupby("Role_Override"):
                label_key = f"Central - {role}"
                raw_results[label_key] = group

    # 7. FILTERS
    all_teams = set()
    all_years = set()
    all_roles = set(raw_results.keys())

    for label, df in raw_results.items():
        if df is None or df.empty: continue
        if "Month" in df.columns: all_years.update(df["Month"].dt.year.unique())
        if "BuildingType" in df.columns:
            if "Role_Override" in df.columns:
                all_teams.update(df["BuildingType"].unique())
            else:
                teams = df["BuildingType"].apply(lambda b: get_team_assignment(label, b)).unique()
                all_teams.update(teams)

    st.sidebar.markdown("---")
    st.sidebar.header("4. Global View Filters")
    selected_roles = st.sidebar.multiselect("Select Roles:", sorted(list(all_roles)), default=sorted(list(all_roles)))
    selected_teams = st.sidebar.multiselect("Select Teams:", sorted(list(all_teams)), default=sorted(list(all_teams)))
    selected_years = st.sidebar.multiselect("Select Years:", sorted(list(all_years)), default=sorted(list(all_years)))

    # 8. APPLY FILTERS
    results = filter_results_by_scope(raw_results, selected_teams, selected_years, selected_roles)

    # 9. DASHBOARD OUTPUTS
    st.markdown("---")
    st.header(f"Consolidated {metric_choice} Plan")

    sum_mx, det_team_mx = build_split_matrices(results, baseline_quantile, metric_col)

    st.subheader("1. Executive Summary")
    if not sum_mx.empty:
        st.dataframe(sum_mx.style.format(fmt), use_container_width=True)
    else:
        st.warning("No data.")

    st.subheader("2. Detailed Breakdown (by Team)")
    if not det_team_mx.empty:
        st.dataframe(det_team_mx.style.format(fmt), use_container_width=True)
    else:
        st.warning("No data.")

    st.subheader("3. Detailed Breakdown (by Building Type)")
    det_bldg_mx = build_building_type_matrix(results, baseline_quantile, metric_col)
    if not det_bldg_mx.empty:
        st.dataframe(det_bldg_mx.style.format(fmt), use_container_width=True)
    else:
        st.info("Building Type view excludes Central Team roles.")

    st.markdown("---")
    st.header(f"Total {metric_choice} by Team (Graph)")
    graph_df = build_team_monthly_data(results, metric_col)
    if not graph_df.empty:
        sel = alt.selection_point(fields=['Team'], bind='legend')
        chart = alt.Chart(graph_df).mark_area().encode(
            x='Month', y=alt.Y(metric_col, title=metric_choice), color='Team',
            opacity=alt.condition(sel, alt.value(1), alt.value(0.2)), tooltip=['Month', 'Team', metric_col]
        ).add_params(sel).properties(height=400)
        st.altair_chart(chart, use_container_width=True)

    st.markdown("---")
    st.header("Resource Breakdown")
    res_df = build_resource_breakdown_data(results, metric_col)
    if not res_df.empty:
        tm = st.selectbox("Inspect Team:", sorted(res_df['Team'].unique()))
        f_res = res_df[res_df['Team'] == tm]
        c_res = alt.Chart(f_res).mark_area().encode(
            x='Month', y=metric_col, color='Discipline', tooltip=['Month', 'Discipline', metric_col]
        ).properties(height=350)
        st.altair_chart(c_res, use_container_width=True)

    st.markdown("---")
    st.header("Project Level Detail")
    colA, colB = st.columns(2)
    with colA:
        p_view = st.radio("Type:", ["Total", "Internal", "Contractor"], horizontal=True)

    proj_mx = build_project_level_matrix(results, baseline_quantile, p_view, metric_col)
    if not proj_mx.empty:
        st.dataframe(proj_mx.style.format(fmt), use_container_width=True)
    else:
        st.info("No project data.")

    st.markdown("---")
    st.header("Quarterly Ramp (Peak Demand)")
    q_years = st.multiselect("Filter Quarter View by Year:", selected_years,
                             default=selected_years[:1] if selected_years else None)
    if q_years:
        q_results = {}
        for k, v in results.items():
            if v is not None and not v.empty:
                q_results[k] = v[v['Month'].dt.year.isin(q_years)]

        ramp_mx = build_quarterly_ramp(q_results, metric_col)
        if not ramp_mx.empty:
            st.dataframe(ramp_mx.style.format(fmt), use_container_width=True)
        else:
            st.info("No data.")

    st.markdown("---")
    st.header("Project Master List")
    pm_df = build_project_master_list(por, known_dates, scenarios, fallback_choice, selected_teams, selected_years)
    st.dataframe(pm_df, use_container_width=True)


if __name__ == "__main__":
    main()