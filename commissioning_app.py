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
        # UPDATED: Group by Project_ID to preserve project-level detail
        df_agg = df.groupby(["Month", "BuildingType", "Project_ID", "Scenario"], as_index=False).agg(
            {"FTE": "sum"}).sort_values("Month")
    else:
        df_agg = pd.DataFrame(columns=["Month", "BuildingType", "Project_ID", "FTE", "Scenario"])

    monthly_by_scen[selected_scen_name] = df_agg
    return monthly_by_scen


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

    def get_team(label, btype):
        if label == "Commissioning Engineer": return "System Integration"
        s = str(btype).upper()
        if "ARS" in s:
            return "ARS Team"
        elif "SSD" in s:
            return "SSD Team"
        elif "IBIS" in s or "AUTOSTORE" in s:
            return "Projects Team"
        return "Other"

    # Process results
    for label, df_monthly in results.items():
        if df_monthly is None or df_monthly.empty: continue

        display_role = disc_map.get(label, label)

        df_work = df_monthly.copy()
        df_work["Team"] = df_work.apply(lambda r: get_team(label, r["BuildingType"]), axis=1)
        df_work["Year"] = df_work["Month"].dt.year

        # Calculate Peak FTE per Year/Team/Role
        monthly_sum = df_work.groupby(["Year", "Month", "Team"], as_index=False)["FTE"].sum()
        annual_peaks = monthly_sum.groupby(["Year", "Team"], as_index=False)["FTE"].max()

        for _, row in annual_peaks.iterrows():
            year = row["Year"]
            team = row["Team"]
            peak_fte = row["FTE"]

            # SPLIT LOGIC
            if display_role == "Installers":
                internal_fte = 0
                contractor_fte = peak_fte
            else:
                internal_fte = peak_fte * baseline_quantile
                contractor_fte = peak_fte - internal_fte

            # --- 1. POPULATE SUMMARY DATA (Category Sums) ---
            if internal_fte > 0.05:
                summary_rows.append({"Category": "Total Internal", "Year": year, "FTE": internal_fte})
            if contractor_fte > 0.05:
                summary_rows.append({"Category": "Total Contractors", "Year": year, "FTE": contractor_fte})

            # --- 2. POPULATE DETAIL DATA (Team/Role) ---
            if internal_fte > 0.05:
                detail_team_rows.append({
                    "Team": team,
                    "Role": display_role,
                    "Year": year,
                    "FTE": internal_fte
                })
            if contractor_fte > 0.05:
                c_role = "Installers" if display_role == "Installers" else "Internal Contractors"
                detail_team_rows.append({
                    "Team": team,
                    "Role": c_role,
                    "Year": year,
                    "FTE": contractor_fte
                })

    # --- BUILD TABLE 1: SUMMARY ---
    if summary_rows:
        sum_df = pd.DataFrame(summary_rows)
        sum_matrix = sum_df.pivot_table(index="Category", columns="Year", values="FTE", aggfunc="sum").fillna(0)
        sum_matrix.loc["Grand Total"] = sum_matrix.sum()
    else:
        sum_matrix = pd.DataFrame()

    # --- BUILD TABLE 2: DETAIL BY TEAM ---
    if detail_team_rows:
        det_df = pd.DataFrame(detail_team_rows)
        det_matrix = det_df.pivot_table(index=["Team", "Role"], columns="Year", values="FTE", aggfunc="sum").fillna(0)
    else:
        det_matrix = pd.DataFrame()

    return sum_matrix, det_matrix


# --- DETAIL BY BUILDING TYPE ---
def build_building_type_matrix(results, baseline_quantile):
    rows = []

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
        df_work["Year"] = df_work["Month"].dt.year

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
                rows.append({
                    "Building Type": btype,
                    "Role": display_role,
                    "Year": year,
                    "FTE": internal_fte
                })
            if contractor_fte > 0.05:
                c_role = "Installers" if display_role == "Installers" else "Internal Contractors"
                rows.append({
                    "Building Type": btype,
                    "Role": c_role,
                    "Year": year,
                    "FTE": contractor_fte
                })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    matrix = df.pivot_table(index=["Building Type", "Role"], columns="Year", values="FTE", aggfunc="sum").fillna(0)
    return matrix


# --- NEW: PROJECT LEVEL DETAIL MATRIX (INCLUDES DATES) ---
def build_project_level_data(results, baseline_quantile, included_roles, view_mode, master_project_df):
    dfs = []

    for label, df in results.items():
        if label not in included_roles or df is None or df.empty:
            continue

        temp = df.copy()
        temp['Year'] = temp['Month'].dt.year

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
        else:  # Total
            temp['Value'] = temp['FTE']

        dfs.append(temp[['Year', 'BuildingType', 'Project_ID', 'Month', 'Value']])

    if not dfs:
        return pd.DataFrame(), pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)

    # 1. Sum across selected roles for each Project/Month (To get Project Curve)
    project_monthly_curve = combined.groupby(['Year', 'BuildingType', 'Project_ID', 'Month'], as_index=False)[
        'Value'].sum()

    # 2. Get Peak Headcount for that project in that year (For Table)
    annual_peak = project_monthly_curve.groupby(['Year', 'BuildingType', 'Project_ID'], as_index=False)['Value'].max()

    # 3. Pivot for Table
    matrix = annual_peak.pivot_table(index=['BuildingType', 'Project_ID'], columns='Year', values='Value',
                                     aggfunc='sum').fillna(0)

    # 4. Merge Go-Live Date
    matrix = matrix.reset_index()
    merged = pd.merge(matrix, master_project_df[['Project ID', 'Go-Live Date']], left_on='Project_ID',
                      right_on='Project ID', how='left')
    merged['Go-Live Date'] = pd.to_datetime(merged['Go-Live Date']).dt.date
    cols = [c for c in matrix.columns if isinstance(c, int)]
    final_cols = ['BuildingType', 'Project_ID', 'Go-Live Date'] + cols

    # --- CONCURRENT PEAK CALCULATION (For Metrics) ---
    # To get True Peak, we must sum ALL projects for a specific month, THEN take max of year
    aggregate_monthly_curve = project_monthly_curve.groupby(['Year', 'Month'], as_index=False)['Value'].sum()
    concurrent_peaks = aggregate_monthly_curve.groupby('Year')['Value'].max()

    return merged[final_cols].set_index(['BuildingType', 'Project_ID', 'Go-Live Date']), concurrent_peaks


def build_team_monthly_data(results):
    all_dfs = []
    if "Commissioning Engineer" in results and results["Commissioning Engineer"] is not None:
        df = results["Commissioning Engineer"].copy()
        df["Team"] = "System Integration"
        all_dfs.append(df[["Month", "Team", "FTE"]])

    non_ce = ["Mechanical Engineer", "Electrical Engineer", "Site Operations", "Site Lead", "Installation"]
    for label in non_ce:
        if label in results and results[label] is not None:
            df = results[label].copy()

            def map_to_team(btype):
                s = str(btype).upper()
                if "ARS" in s: return "ARS Team"
                if "SSD" in s: return "SSD Team"
                if "IBIS" in s or "AUTOSTORE" in s: return "Projects Team"
                return "Other"

            df["Team"] = df["BuildingType"].apply(map_to_team)
            all_dfs.append(df[["Month", "Team", "FTE"]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team"], as_index=False)["FTE"].sum()


def build_resource_breakdown_data(results):
    all_dfs = []
    disc_map = {"Commissioning Engineer": "CE", "Mechanical Engineer": "ME", "Electrical Engineer": "EE",
                "Site Operations": "Ops", "Site Lead": "Lead", "Installation": "Install"}

    if "Commissioning Engineer" in results and results["Commissioning Engineer"] is not None:
        df = results["Commissioning Engineer"].copy()
        df["Team"] = "System Integration"
        df["Discipline"] = "CE"
        all_dfs.append(df[["Month", "Team", "Discipline", "FTE"]])

    non_ce = ["Mechanical Engineer", "Electrical Engineer", "Site Operations", "Site Lead", "Installation"]
    for label in non_ce:
        if label in results and results[label] is not None:
            df = results[label].copy()

            def map_to_team(btype):
                s = str(btype).upper()
                if "ARS" in s: return "ARS Team"
                if "SSD" in s: return "SSD Team"
                if "IBIS" in s or "AUTOSTORE" in s: return "Projects Team"
                return "Other"

            df["Team"] = df["BuildingType"].apply(map_to_team)
            df["Discipline"] = disc_map.get(label, label)
            all_dfs.append(df[["Month", "Team", "Discipline", "FTE"]])

    if not all_dfs: return pd.DataFrame()
    return pd.concat(all_dfs, ignore_index=True).groupby(["Month", "Team", "Discipline"], as_index=False)["FTE"].sum()


def build_project_master_list(por, known_dates, scenarios, fallback_scen_name):
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
        for y in years:
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

    st.sidebar.header("3. Internal Staffing Strategy")
    # CHANGED DEFAULT TO P100 (Index 3)
    baseline_choice = st.sidebar.selectbox(
        "Baseline Headcount Rule",
        ["Lean (P50)", "Moderate (P70)", "Robust (P90)", "Max (P100)"],
        index=3
    )
    b_map = {"Lean (P50)": 0.5, "Moderate (P70)": 0.7, "Robust (P90)": 0.9, "Max (P100)": 1.0}
    baseline_quantile = b_map[baseline_choice]

    if selected_scen != 'HYBRID':
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == selected_scen].copy()
    else:
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == fallback_choice].copy()

    disciplines_map = {
        "Commissioning Engineer": ("CE", "CE"),
        "Mechanical Engineer": ("ME", "ME"),
        "Electrical Engineer": ("EE", "EE"),
        "Site Operations": ("SITEOPS", "SITEOPS"),
        "Site Lead": ("LEAD", "LEAD"),
        "Installation": ("INSTALL", "INSTALL")
    }

    results = {}
    for label, (prefix, sheet_key) in disciplines_map.items():
        df_assump = assumptions_dict.get(sheet_key)
        if df_assump is not None:
            results[label] = build_monthly_labor_detailed(
                por, df_assump, filtered_scenarios, prefix,
                known_dates, fallback_choice, selected_scen
            ).get(selected_scen)

    # ---------------- SUMMARY TABLES ----------------
    st.header("Consolidated Hiring Plan")
    st.caption(
        f"Strategy: {baseline_choice}. Values represent **Net Concurrent Demand** (accounting for schedule staggering).")

    summary_df, detail_team_df = build_split_matrices(results, baseline_quantile)

    st.subheader("1. Executive Summary (Internal vs Contractor)")
    if not summary_df.empty:
        st.dataframe(summary_df.style.format("{:.1f}"), use_container_width=True)
    else:
        st.warning("No summary data available.")

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

    # ---------------- NEW: DETAILED PROJECT VIEW ----------------
    st.header("4. Detailed Project View (Gross Demand)")
    st.caption(
        "Inspect headcount by specific project. Note: Summing these values = **Gross Demand**, which may exceed Net Demand due to schedule overlap.")

    # GENERATE MASTER LIST FIRST to get Dates
    project_master_df = build_project_master_list(por, known_dates, scenarios, fallback_choice)

    col1, col2 = st.columns([1, 2])
    with col1:
        view_mode = st.radio("Headcount Type:", ["Total", "Internal", "Contractor"], horizontal=True)
    with col2:
        available_roles = list(results.keys())
        selected_roles = st.multiselect("Filter Job Roles:", available_roles, default=available_roles)

    if not project_master_df.empty:
        project_level_df, concurrent_peaks = build_project_level_data(results, baseline_quantile, selected_roles,
                                                                      view_mode, project_master_df)

        if not project_level_df.empty:
            # SHOW METRICS FOR COMPARISON
            year_cols = [c for c in project_level_df.columns if isinstance(c, int)]
            if year_cols:
                disp_year = year_cols[0]
                sum_of_peaks = project_level_df[disp_year].sum()
                true_peak = concurrent_peaks.get(disp_year, 0)

                m1, m2 = st.columns(2)
                m1.metric(f"Total Gross Demand (Sum of Peaks) in {disp_year}", f"{sum_of_peaks:.1f}",
                          help="Simple sum of all project requirements.")
                m2.metric(f"Net Concurrent Demand (True Peak) in {disp_year}", f"{true_peak:.1f}",
                          delta=f"{sum_of_peaks - true_peak:.1f} Saved via Staggering", delta_color="inverse",
                          help="Actual headcount needed accounting for staggering.")

            st.dataframe(project_level_df.style.format("{:.1f}"), use_container_width=True)
        else:
            st.info("No data for the selected filters.")
    else:
        st.warning("POR is empty.")

    st.markdown("---")

    # ---------------- RESOURCE BREAKDOWN BY TEAM ----------------
    st.header("Resource Breakdown by Team (Graph)")
    resource_df = build_resource_breakdown_data(results)
    if not resource_df.empty:
        all_teams = sorted(resource_df['Team'].unique())
        selected_team = st.selectbox("Select Team to Inspect", all_teams)
        filtered_res = resource_df[resource_df['Team'] == selected_team]
        chart_res = alt.Chart(filtered_res).mark_area().encode(
            x='Month', y='FTE', color=alt.Color('Discipline', scale=alt.Scale(scheme='set2')),
            tooltip=['Month', 'Discipline', 'FTE']
        ).properties(title=f"Role Composition for {selected_team}", height=350)
        st.altair_chart(chart_res, use_container_width=True)

    st.markdown("---")

    # ---------------- PROJECT MASTER LIST ----------------
    st.header("Project Master List (POR Detail)")
    if not project_master_df.empty:
        st.dataframe(project_master_df, use_container_width=True)
    else:
        st.info("No projects found in POR.")

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