import pandas as pd
import streamlit as st
import altair as alt

# Set Streamlit page config
st.set_page_config(page_title="MHE Staffing Consolidated Demand", layout="wide")


# ---------------------------------------------------
# CORE MODEL FUNCTIONS
# ---------------------------------------------------

def load_inputs_from_excel(excel_source):
    """Loads POR, Scenarios, Assumptions, and the new KNOWN_DATES sheet."""
    known_dates = pd.DataFrame(columns=['BuildingType', 'Year', 'Project_ID', 'Go-Live Date'])

    try:
        xl = pd.ExcelFile(excel_source)
    except Exception:
        return None, None, None, known_dates

    try:
        # Load Mandatory Sheets
        por = pd.read_excel(xl, sheet_name="POR")
        scenarios = pd.read_excel(xl, sheet_name="Scenario_Params")

        # Load KNOWN_DATES Sheet (if it exists)
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
        else:
            st.info("KNOWN_DATES sheet not found. Hybrid Scenario unavailable.")

    except ValueError as e:
        if "KNOWN_DATES" not in str(e):
            st.error(f"Error loading core sheets: {e}")
            raise e

    # Load Discipline Assumption Sheets
    assumptions_dict = {}
    sheets_to_load = {
        "CE": "CE_Assumptions",
        "ME": "ME_Assumptions",
        "EE": "EE_Assumptions",
        "SITEOPS": "SITEOPS_Assumptions",
        "LEAD": "LEAD_Assumptions",
        "INSTALL": "Install_Assumptions"
    }

    for key, sheet_name in sheets_to_load.items():
        if sheet_name in xl.sheet_names:
            try:
                assumptions_dict[key] = pd.read_excel(xl, sheet_name=sheet_name)
            except Exception as e:
                st.warning(f"Error loading sheet {sheet_name}: {e}")

    return por, scenarios, assumptions_dict, known_dates


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
        span = end_m - start_m + 1

        if q_count > 0:
            step = span / q_count
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
    # Robust Year Identification
    year_map = {}
    for c in por.columns:
        if str(c).isdigit():
            year_map[int(c)] = c
    years = sorted(year_map.keys())

    if not years or assumptions_df.empty:
        return {}

    eff_map = build_efficiency_map(assumptions_df, years)
    assumptions_idx = assumptions_df.set_index("BuildingType")

    staff_col = f"{prefix}_Staff_Per_Launch"
    dur_col = f"{prefix}_Duration_Months"
    lead_col = f"{prefix}_Lead_Months"

    if staff_col not in assumptions_idx.columns:
        return {}

    monthly_by_scen = {}
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
            if key not in known_dates_map:
                known_dates_map[key] = []
            known_dates_map[key].append((kd_row['Project_ID'], kd_row['Go-Live Date']))

    for _, row in por.iterrows():
        btype = row["BuildingType"]
        if btype not in assumptions_idx.index: continue

        try:
            staff_per_launch = float(assumptions_idx.loc[btype, staff_col])
            duration = int(assumptions_idx.loc[btype, dur_col])
            lead_time = int(assumptions_idx.loc[btype, lead_col])
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
                known_id, go_live_date = known_launches[i]
                project_details.append({'Go_Live_Date': go_live_date, 'Project_ID': known_id})
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue
                gy, gm = go_live_months[i]
                go_live_date = pd.Timestamp(year=gy, month=gm, day=1)
                dummy_id = f"{btype}-{y}-{i + 1}"
                project_details.append({'Go_Live_Date': go_live_date, 'Project_ID': dummy_id})

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
                    rows.append({
                        "Scenario": selected_scen_name,
                        "Month": dt,
                        "BuildingType": btype,
                        "FTE": fte,
                        "Active_Projects": 1,
                        "Project_ID": project_id
                    })

    if rows:
        df = pd.DataFrame(rows)
        # Group by Project_ID to preserve detail
        df_agg = df.groupby(["Month", "BuildingType", "Project_ID", "Scenario"], as_index=False).agg(
            {"FTE": "sum"}).sort_values("Month")
    else:
        df_agg = pd.DataFrame(columns=["Month", "BuildingType", "Project_ID", "FTE", "Scenario"])

    monthly_by_scen[selected_scen_name] = df_agg
    return monthly_by_scen


# --- HELPER: ROBUST TEAM ASSIGNMENT ---
def get_team_assignment(role, btype):
    # Logic: Role overrides Building Type for SIF
    if role == "Commissioning Engineer":
        return "System Integration"

    # Otherwise check building type
    s = str(btype).upper()
    if "ARS" in s: return "ARS Team"
    if "SSD" in s: return "SSD Team"
    if "IBIS" in s or "AUTOSTORE" in s: return "Projects Team"
    return "Other"


# --- HELPER: GLOBAL FILTER ---
def filter_results_by_scope(results, selected_teams, selected_years, selected_roles):
    filtered = {}
    for label, df in results.items():
        # 1. Filter by Role Name
        if label not in selected_roles:
            continue

        if df is None or df.empty:
            filtered[label] = df
            continue

        # Add derived columns for filtering
        df_work = df.copy()
        # Use robust team logic passing the Label (Role)
        df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))
        df_work["Year"] = df_work["Month"].dt.year

        # 2. Filter by Team and Year
        mask = (df_work["Team"].isin(selected_teams)) & (df_work["Year"].isin(selected_years))

        # Keep only matching rows
        filtered_df = df_work[mask]

        # Only add to results if data remains (or if you want to keep keys but empty data)
        if not filtered_df.empty:
            filtered[label] = filtered_df

    return filtered


# --- SPLIT INTO TABLES (SUMMARY, DETAIL-TEAM, DETAIL-BUILDING) ---
def build_split_matrices(results, baseline_quantile):
    summary_rows = []
    detail_team_rows = []

    disc_map = {
        "Commissioning Engineer": "CE",
        "Mechanical Engineer": "ME",
        "Electrical Engineer": "EE",
        "Site Operations": "Ops Lead",
        "Site Lead": "Site Lead",
        "Installation": "Installers"
    }

    for label, df_monthly in results.items():
        if df_monthly is None or df_monthly.empty: continue

        display_role = disc_map.get(label, label)

        df_work = df_monthly.copy()
        if "Team" not in df_work.columns:
            df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))
        if "Year" not in df_work.columns:
            df_work["Year"] = df_work["Month"].dt.year

        monthly_sum = df_work.groupby(["Year", "Month", "Team"], as_index=False)["FTE"].sum()
        annual_peaks = monthly_sum.groupby(["Year", "Team"], as_index=False)["FTE"].max()

        for _, row in annual_peaks.iterrows():
            year = row["Year"]
            team = row["Team"]
            peak_fte = row["FTE"]

            if display_role == "Installers":
                internal_fte = 0
                contractor_fte = peak_fte
            else:
                internal_fte = peak_fte * baseline_quantile
                contractor_fte = peak_fte - internal_fte

            if internal_fte > 0.05:
                summary_rows.append({"Category": "Total Internal", "Year": year, "FTE": internal_fte})
                detail_team_rows.append({"Team": team, "Role": display_role, "Year": year, "FTE": internal_fte})

            if contractor_fte > 0.05:
                summary_rows.append({"Category": "Total Contractors", "Year": year, "FTE": contractor_fte})
                c_role = "Installers" if display_role == "Installers" else "Internal Contractors"
                detail_team_rows.append({"Team": team, "Role": c_role, "Year": year, "FTE": contractor_fte})

    if summary_rows:
        sum_df = pd.DataFrame(summary_rows)
        sum_matrix = sum_df.pivot_table(index="Category", columns="Year", values="FTE", aggfunc="sum").fillna(0)
        sum_matrix = sum_matrix.sort_index(ascending=False)
        sum_matrix.loc["Grand Total"] = sum_matrix.sum()
    else:
        sum_matrix = pd.DataFrame()

    if detail_team_rows:
        det_df = pd.DataFrame(detail_team_rows)
        det_matrix = det_df.pivot_table(index=["Team", "Role"], columns="Year", values="FTE", aggfunc="sum").fillna(0)
    else:
        det_matrix = pd.DataFrame()

    return sum_matrix, det_matrix


# --- DETAIL BY BUILDING TYPE ---
def build_building_type_matrix(results, baseline_quantile):
    rows = []
    disc_map = {"Commissioning Engineer": "CE", "Mechanical Engineer": "ME", "Electrical Engineer": "EE",
                "Site Operations": "Ops Lead", "Site Lead": "Site Lead", "Installation": "Installers"}

    for label, df_monthly in results.items():
        if df_monthly is None or df_monthly.empty: continue
        display_role = disc_map.get(label, label)
        df_work = df_monthly.copy()
        if "Year" not in df_work.columns: df_work["Year"] = df_work["Month"].dt.year

        monthly_sum = df_work.groupby(["Year", "Month", "BuildingType"], as_index=False)["FTE"].sum()
        annual_peaks = monthly_sum.groupby(["Year", "BuildingType"], as_index=False)["FTE"].max()

        for _, row in annual_peaks.iterrows():
            year = row["Year"]
            btype = row["BuildingType"]
            peak_fte = row["FTE"]

            if display_role == "Installers":
                internal_fte = 0
                contractor_fte = peak_fte
            else:
                internal_fte = peak_fte * baseline_quantile
                contractor_fte = peak_fte - internal_fte

            if internal_fte > 0.05:
                rows.append({"Building Type": btype, "Role": display_role, "Year": year, "FTE": internal_fte})
            if contractor_fte > 0.05:
                c_role = "Installers" if display_role == "Installers" else "Internal Contractors"
                rows.append({"Building Type": btype, "Role": c_role, "Year": year, "FTE": contractor_fte})

    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows)
    matrix = df.pivot_table(index=["Building Type", "Role"], columns="Year", values="FTE", aggfunc="sum").fillna(0)
    return matrix


# --- PROJECT LEVEL DETAIL MATRIX ---
def build_project_level_matrix(results, baseline_quantile, view_mode):
    dfs = []
    for label, df in results.items():
        if df is None or df.empty: continue
        temp = df.copy()
        if "Year" not in temp.columns: temp['Year'] = temp['Month'].dt.year

        if label == "Installation":
            temp['Internal'] = 0
            temp['Contractor'] = temp['FTE']
        else:
            temp['Internal'] = temp['FTE'] * baseline_quantile
            temp['Contractor'] = temp['FTE'] - temp['Internal']

        if view_mode == "Internal":
            temp['Value'] = temp['Internal']
        elif view_mode == "Contractor":
            temp['Value'] = temp['Contractor']
        else:
            temp['Value'] = temp['FTE']

        dfs.append(temp[['Year', 'BuildingType', 'Project_ID', 'Month', 'Value']])

    if not dfs: return pd.DataFrame()
    combined = pd.concat(dfs, ignore_index=True)
    monthly_sum = combined.groupby(['Year', 'BuildingType', 'Project_ID', 'Month'], as_index=False)['Value'].sum()
    annual_peak = monthly_sum.groupby(['Year', 'BuildingType', 'Project_ID'], as_index=False)['Value'].max()
    matrix = annual_peak.pivot_table(index=['BuildingType', 'Project_ID'], columns='Year', values='Value',
                                     aggfunc='sum').fillna(0)
    return matrix


# --- QUARTERLY RAMP TABLE ---
def build_quarterly_ramp(results):
    rows = []
    disc_map = {"Commissioning Engineer": "CE", "Mechanical Engineer": "ME", "Electrical Engineer": "EE",
                "Site Operations": "Ops", "Site Lead": "Lead", "Installation": "Install"}

    for label, df in results.items():
        if df is None or df.empty: continue
        role_name = disc_map.get(label, label)
        df_work = df.copy()
        df_work['Quarter'] = df_work['Month'].dt.to_period('Q').astype(str)
        if "Team" not in df_work.columns:
            df_work["Team"] = df_work["BuildingType"].apply(lambda b: get_team_assignment(label, b))

        monthly_curve = df_work.groupby(['Quarter', 'Month', 'Team'], as_index=False)['FTE'].sum()
        quarterly_peak = monthly_curve.groupby(['Quarter', 'Team'], as_index=False)['FTE'].max()

        for _, row in quarterly_peak.iterrows():
            rows.append({"Team": row['Team'], "Role": role_name, "Quarter": row['Quarter'], "FTE": row['FTE']})

    if not rows: return pd.DataFrame()
    df_ramp = pd.DataFrame(rows)
    matrix = df_ramp.pivot_table(index=["Team", "Role"], columns="Quarter", values="FTE", aggfunc="sum").fillna(0)
    cols = sorted(matrix.columns)
    return matrix[cols]


def build_team_monthly_data(results):
    all_dfs = []
    for label, df in results.items():
        if df is None or df.empty: continue
        temp = df.copy()
        if "Team" not in temp.columns:
            temp["Team"] = temp["BuildingType"].apply(lambda b: get_team_assignment(label, b))
        all_dfs.append(temp[["Month", "Team", "FTE"]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team"], as_index=False)["FTE"].sum()


def build_resource_breakdown_data(results):
    all_dfs = []
    disc_map = {"Commissioning Engineer": "CE", "Mechanical Engineer": "ME", "Electrical Engineer": "EE",
                "Site Operations": "Ops", "Site Lead": "Lead", "Installation": "Install"}

    for label, df in results.items():
        if df is None or df.empty: continue
        temp = df.copy()
        if "Team" not in temp.columns:
            temp["Team"] = temp["BuildingType"].apply(lambda b: get_team_assignment(label, b))
        temp["Discipline"] = disc_map.get(label, label)
        all_dfs.append(temp[["Month", "Team", "Discipline", "FTE"]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team", "Discipline"], as_index=False)["FTE"].sum()


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
        # NOTE: Using a generic role here just to get the Team classification
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
    st.title("MHE Integration Staffing Plan")

    st.sidebar.header("1. Input Data Source")
    uploaded_file = st.sidebar.file_uploader("Upload commissioning_input.xlsx", type=["xlsx"])

    if uploaded_file is None:
        st.warning("Please upload your input Excel file to begin.")
        return

    por, scenarios, assumptions_dict, known_dates = load_inputs_from_excel(uploaded_file)
    if por is None or scenarios is None:
        st.error("Error loading core data.")
        return

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

        # --- MOVED FILTERED SCENARIOS DEFINITION HERE ---
    if selected_scen != 'HYBRID':
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == selected_scen].copy()
    else:
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == fallback_choice].copy()

    st.sidebar.header("3. Internal Staffing Strategy")
    # CHANGED DEFAULT TO P100 (Index 3)
    baseline_choice = st.sidebar.selectbox(
        "Baseline Headcount Rule",
        ["Lean (P50)", "Moderate (P70)", "Robust (P90)", "Max (P100)"],
        index=3
    )
    b_map = {"Lean (P50)": 0.5, "Moderate (P70)": 0.7, "Robust (P90)": 0.9, "Max (P100)": 1.0}
    baseline_quantile = b_map[baseline_choice]

    disciplines_map = {
        "Commissioning Engineer": ("CE", "CE"),
        "Mechanical Engineer": ("ME", "ME"),
        "Electrical Engineer": ("EE", "EE"),
        "Site Operations": ("SITEOPS", "SITEOPS"),
        "Site Lead": ("LEAD", "LEAD"),
        "Installation": ("INSTALL", "INSTALL")
    }

    # 1. GENERATE FULL RAW DATA
    raw_results = {}
    for label, (prefix, sheet_key) in disciplines_map.items():
        df_assump = assumptions_dict.get(sheet_key)
        if df_assump is not None:
            raw_results[label] = build_monthly_labor_detailed(
                por, df_assump, filtered_scenarios, prefix,
                known_dates, fallback_choice, selected_scen
            ).get(selected_scen)

    # 2. DETERMINE FILTERS
    all_teams = set()
    all_years = set()
    all_roles = set(raw_results.keys())  # All loaded roles

    for label, df in raw_results.items():
        if df is None or df.empty: continue
        if "Month" in df.columns: all_years.update(df["Month"].dt.year.unique())
        if "BuildingType" in df.columns:
            # Pass the ROLE (label) so "System Integration" is detected correctly
            teams = df["BuildingType"].apply(lambda b: get_team_assignment(label, b)).unique()
            all_teams.update(teams)

    st.sidebar.markdown("---")
    st.sidebar.header("4. Global View Filters")
    selected_roles = st.sidebar.multiselect("Select Roles to View:", sorted(list(all_roles)),
                                            default=sorted(list(all_roles)))
    selected_teams = st.sidebar.multiselect("Select Teams to View:", sorted(list(all_teams)),
                                            default=sorted(list(all_teams)))
    selected_years = st.sidebar.multiselect("Select Years to View:", sorted(list(all_years)),
                                            default=sorted(list(all_years)))

    # 3. APPLY FILTERS
    results = filter_results_by_scope(raw_results, selected_teams, selected_years, selected_roles)

    # ---------------- SUMMARY TABLES ----------------
    st.header("Consolidated Hiring Plan")
    st.caption(f"Strategy: {baseline_choice}. (Installers are always allocated to Contractors).")

    summary_df, detail_team_df = build_split_matrices(results, baseline_quantile)

    st.subheader("1. Executive Summary (Internal vs Contractor)")
    if not summary_df.empty:
        st.dataframe(summary_df.style.format("{:.1f}"), use_container_width=True)
    else:
        st.warning("No summary data available (check filters).")

    st.subheader("2. Detailed Role Breakdown (by Team)")
    if not detail_team_df.empty:
        st.dataframe(detail_team_df.style.format("{:.1f}"), use_container_width=True)
    else:
        st.warning("No detailed team data available.")

    st.subheader("3. Detailed Role Breakdown (by Building Type)")
    detail_building_df = build_building_type_matrix(results, baseline_quantile)
    if not detail_building_df.empty:
        st.dataframe(detail_building_df.style.format("{:.1f}"), use_container_width=True)
    else:
        st.warning("No detailed building type data available.")

    st.markdown("---")

    # ---------------- TOTAL HEADCOUNT CHART ----------------
    st.header("Total Headcount by Team (Graph)")
    team_monthly_df = build_team_monthly_data(results)
    if not team_monthly_df.empty:
        selection = alt.selection_point(fields=['Team'], bind='legend')
        chart_team = alt.Chart(team_monthly_df).mark_area().encode(
            x='Month', y='FTE', color=alt.Color('Team', scale=alt.Scale(scheme='category10')),
            tooltip=['Month', 'Team', 'FTE'], opacity=alt.condition(selection, alt.value(1), alt.value(0.2))
        ).add_params(selection).properties(height=400)
        st.altair_chart(chart_team, use_container_width=True)

    st.markdown("---")

    # ---------------- RESOURCE BREAKDOWN BY TEAM ----------------
    st.header("Resource Breakdown by Team (Graph)")
    resource_df = build_resource_breakdown_data(results)
    if not resource_df.empty:
        avail_teams_for_graph = sorted(resource_df['Team'].unique())
        selected_team_graph = st.selectbox("Select Team to Inspect", avail_teams_for_graph)
        filtered_res = resource_df[resource_df['Team'] == selected_team_graph]
        chart_res = alt.Chart(filtered_res).mark_area().encode(
            x='Month', y='FTE', color=alt.Color('Discipline', scale=alt.Scale(scheme='set2')),
            tooltip=['Month', 'Discipline', 'FTE']
        ).properties(title=f"Role Composition for {selected_team_graph}", height=350)
        st.altair_chart(chart_res, use_container_width=True)

    st.markdown("---")

    # ---------------- NEW: DETAILED PROJECT VIEW ----------------
    st.header("4. Detailed Project View")
    col1, col2 = st.columns(2)
    with col1:
        view_mode = st.radio("Headcount Type:", ["Total", "Internal", "Contractor"], horizontal=True)

    project_level_df = build_project_level_matrix(results, baseline_quantile, view_mode)
    if not project_level_df.empty:
        st.dataframe(project_level_df.style.format("{:.1f}"), use_container_width=True)
    else:
        st.info("No data for the selected filters.")

    st.markdown("---")

    # ---------------- NEW: QUARTERLY HIRING RAMP ----------------
    st.header("5. Quarterly Hiring Ramp (Peak Demand)")
    st.caption("Shows the Peak headcount needed in each Quarter to handle the workload.")

    q_years = st.multiselect("Filter Quarterly Table by Year:", selected_years,
                             default=selected_years[:1] if selected_years else None)

    if q_years:
        # Re-filter results just for these years
        q_results = filter_results_by_scope(raw_results, selected_teams, q_years, selected_roles)
        ramp_df = build_quarterly_ramp(q_results)
        if not ramp_df.empty:
            st.dataframe(ramp_df.style.format("{:.1f}"), use_container_width=True)
        else:
            st.info("No data for selected years.")
    else:
        st.info("Please select at least one year to view the Quarterly Ramp.")

    st.markdown("---")

    # ---------------- PROJECT MASTER LIST ----------------
    st.header("Project Master List (POR Detail)")
    project_master_df = build_project_master_list(por, known_dates, scenarios, fallback_choice, selected_teams,
                                                  selected_years)
    if not project_master_df.empty:
        st.dataframe(project_master_df, use_container_width=True)
    else:
        st.info("No projects found in POR (check filters).")

    st.markdown("---")

    # ---------------- ASSUMPTIONS ----------------
    with st.expander("Show/Hide Input Assumptions"):
        st.subheader("Core Assumptions")
        st.dataframe(por)
        st.dataframe(scenarios)
        for k, v in assumptions_dict.items():
            st.write(f"**{k} Assumptions**")
            st.dataframe(v)
        if not known_dates.empty:
            st.write("**Known Dates**")
            st.dataframe(known_dates)


if __name__ == "__main__":
    main()