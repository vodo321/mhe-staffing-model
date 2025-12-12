import pandas as pd
import streamlit as st
import altair as alt
from pathlib import Path

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
            # RENAME COLUMN IF USER KEPT 'Start_Date' by mistake, otherwise expect 'Go-Live Date'
            if 'Start_Date' in known_dates.columns and 'Go-Live Date' not in known_dates.columns:
                known_dates = known_dates.rename(columns={'Start_Date': 'Go-Live Date'})

            known_dates['Go-Live Date'] = pd.to_datetime(known_dates['Go-Live Date'])

            # Ensure required columns exist, adding placeholders if necessary
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
        "SAFETY": "SAFETY_Assumptions",
        "LEAD": "LEAD_Assumptions"
    }

    for key, sheet_name in sheets_to_load.items():
        if sheet_name in xl.sheet_names:
            try:
                assumptions_dict[key] = pd.read_excel(xl, sheet_name=sheet_name)
            except Exception as e:
                st.warning(f"Error loading sheet {sheet_name}: {e}")

    return por, scenarios, assumptions_dict, known_dates


def build_efficiency_map(assumptions_df: pd.DataFrame, years):
    """Calculates efficiency factors based on annual improvement."""
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
    """Distributes annual project count into monthly start dates based on quarterly share."""
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

        # Calculate the step to ensure equal spacing within the quarter's month range
        if q_count > 0:
            step = span / q_count
            for k in range(q_count):
                # The Go-Live month is deterministic (first day of the month)
                target_month = start_m + int(k * step)
                if target_month > end_m: target_month = end_m
                months.append((year, target_month))

    return months  # Returns a list of (year, month) tuples (Go-Live dates)


def build_monthly_labor_detailed(por: pd.DataFrame,
                                 assumptions_df: pd.DataFrame,
                                 scenarios: pd.DataFrame,
                                 prefix: str,
                                 known_dates: pd.DataFrame,
                                 fallback_scen_name: str,
                                 selected_scen_name: str):
    year_cols = [c for c in por.columns if str(c).isdigit()]
    years = [int(y) for y in year_cols]

    if not years or assumptions_df.empty:
        return {}

    eff_map = build_efficiency_map(assumptions_df, years)
    assumptions_idx = assumptions_df.set_index("BuildingType")

    staff_col = f"{prefix}_Staff_Per_Launch"
    dur_col = f"{prefix}_Duration_Months"
    lead_col = f"{prefix}_Lead_Months"

    if staff_col not in assumptions_idx.columns:
        if "CE_Staff_Per_Launch" in assumptions_idx.columns:
            staff_col = "CE_Staff_Per_Launch"
            dur_col = "CE_Duration_Months"
            lead_col = "CE_Lead_Months"
        else:
            return {}

    # INITIALIZE THE DICTIONARY (This was the missing line causing the error)
    monthly_by_scen = {}

    # Use the selected scenario if it's not Hybrid, otherwise use the fallback
    scen_to_use = selected_scen_name if selected_scen_name != 'HYBRID' else fallback_scen_name
    scenario_row = scenarios[scenarios['ScenarioName'] == scen_to_use].iloc[0]
    q_shares = {k: scenario_row.get(f"{k}_Share", 0.0) for k in ["Q1", "Q2", "Q3", "Q4"]}

    rows = []

    # Map known dates for quick lookup
    # Key: (BuildingType, Year) -> List of Project_ID and Go-Live Date tuples
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
            launches = row[str(y)]
            if pd.isna(launches) or launches == 0: continue
            launches = int(launches)

            # Use the default quarterly based distribution for Fallback Go-Live dates
            go_live_months = assign_go_live_months_quarter_based(launches, int(y), q_shares)

            project_details = []

            known_launches = known_dates_map.get((btype, int(y)), [])
            num_known = len(known_launches)

            # --- 1. Process Known Projects (Fixed Go-Live from Input) ---
            for i in range(num_known):
                known_id, go_live_date = known_launches[i]

                project_details.append({
                    'Go_Live_Date': go_live_date,
                    'Project_ID': known_id
                })

            # --- 2. Process Unknown Projects (Fallback Go-Live from Scenario) ---
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue

                gy, gm = go_live_months[i]

                # Assign to 1st of the assigned month
                go_live_date = pd.Timestamp(year=gy, month=gm, day=1)

                # Create Dummy ID
                dummy_id = f"{btype}-{y}-{i + 1}"

                project_details.append({
                    'Go_Live_Date': go_live_date,
                    'Project_ID': dummy_id
                })

            # --- CALCULATE MONTHLY FTE BACKWARDS FROM GO-LIVE ---

            for detail in project_details:
                go_live_date = detail['Go_Live_Date']
                project_id = detail['Project_ID']

                # Calculate Start Date: Go-Live minus Lead Time
                start_date = go_live_date - pd.DateOffset(months=lead_time)

                # Duration starts from Start Date
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
            # We keep BuildingType and Scenario in the aggregation
            df_agg = (
                df.groupby(["Month", "BuildingType", "Scenario"], as_index=False)
                .agg({"FTE": "sum", "Active_Projects": "sum"})
                .sort_values("Month")
            )
        else:
            df_agg = pd.DataFrame(columns=["Month", "BuildingType", "FTE", "Active_Projects", "Scenario"])

        monthly_by_scen[selected_scen_name] = df_agg

    return monthly_by_scen


def build_total_annual_summary(monthly_df, baseline_method, baseline_quantile):
    """
    Calculates staffing metrics based on the user's definition: Internal Base = Percentage of Total Peak FTE.
    """
    if monthly_df.empty:
        return pd.DataFrame()

    total_df = monthly_df.groupby("Month", as_index=False)["FTE"].sum()
    total_df["Year"] = total_df["Month"].dt.year

    rows = []
    for year, series in total_df.groupby("Year")["FTE"]:
        peak_fte = series.max()
        avg_fte = series.mean()

        if baseline_method == "avg":
            internal_baseline = avg_fte
        else:
            internal_baseline = peak_fte * baseline_quantile

        contractor_fte = max(peak_fte - internal_baseline, 0.0)

        rows.append({
            "Year": int(year),
            "Avg_FTE": avg_fte,
            "Internal_Base": internal_baseline,
            "Total_Peak": peak_fte,
            "Contractor_Peak": contractor_fte,
        })

    return pd.DataFrame(rows).set_index("Year")


# ---------------------------------------------------
# CONSOLIDATED SUMMARY FUNCTIONS
# ---------------------------------------------------

def build_por_summary(por: pd.DataFrame):
    """Summarizes total launches per year from the POR."""
    year_cols = [c for c in por.columns if str(c).isdigit()]
    por_melt = por.melt(id_vars=["BuildingType"], value_vars=year_cols, var_name="Year", value_name="Launches")
    por_melt["Year"] = por_melt["Year"].astype(int)
    return por_melt.groupby("Year")["Launches"].sum().reset_index().rename(columns={"Launches": "Total_Launches"})


def build_consolidated_report(por_summary_df, annual_summaries: dict, selected_scen):
    df = por_summary_df.copy()

    # Updated list of all prefixes
    all_prefixes = ["CE", "ME", "EE", "SAFETY", "LEAD"]

    for prefix in all_prefixes:
        if prefix in annual_summaries and not annual_summaries[prefix].empty:
            annual_df = annual_summaries[prefix].reset_index()[
                ["Year", "Total_Peak", "Internal_Base", "Contractor_Peak", "Avg_FTE"]]

            annual_df = annual_df.rename(columns={
                "Total_Peak": f"{prefix}_Peak",
                "Internal_Base": f"{prefix}_Internal",
                "Contractor_Peak": f"{prefix}_Contractor",
                "Avg_FTE": f"{prefix}_Avg"
            })

            annual_df["Year"] = annual_df["Year"].astype(int)
            df = pd.merge(df, annual_df, on="Year", how="left")

    df["Scenario"] = selected_scen
    df = df.set_index("Year").fillna(0)

    # Define and filter for desired columns
    desired_cols = ["Total_Launches"]
    for prefix in all_prefixes:
        desired_cols.extend([f"{prefix}_Internal", f"{prefix}_Peak", f"{prefix}_Contractor"])

    df = df[df.columns.intersection(desired_cols)]

    # DYNAMIC MultiIndex Construction
    new_cols_dynamic = []

    col_to_tuple = {
        "Total_Launches": ("POR", "Total_Launches"),
        "CE_Internal": ("CE", "Internal_Base"),
        "CE_Peak": ("CE", "Peak_FTE"),
        "CE_Contractor": ("CE", "Contractor_Need"),
        "ME_Internal": ("ME", "Internal_Base"),
        "ME_Peak": ("ME", "Peak_FTE"),
        "ME_Contractor": ("ME", "Contractor_Need"),
        "EE_Internal": ("EE", "Internal_Base"),
        "EE_Peak": ("EE", "Peak_FTE"),
        "EE_Contractor": ("EE", "Contractor_Need"),
        "SAFETY_Internal": ("SAFETY", "Internal_Base"),
        "SAFETY_Peak": ("SAFETY", "Peak_FTE"),
        "SAFETY_Contractor": ("SAFETY", "Contractor_Need"),
        "LEAD_Internal": ("LEAD", "Internal_Base"),
        "LEAD_Peak": ("LEAD", "Peak_FTE"),
        "LEAD_Contractor": ("LEAD", "Contractor_Need"),
    }

    for col in df.columns:
        if col in col_to_tuple:
            new_cols_dynamic.append(col_to_tuple[col])

    df.columns = pd.MultiIndex.from_tuples(new_cols_dynamic)

    # ADD ROW TOTALS
    df[("TOTAL", "Internal_Base")] = df.loc[:, (slice(None), 'Internal_Base')].sum(axis=1)
    df[("TOTAL", "Peak_FTE")] = df.loc[:, (slice(None), 'Peak_FTE')].sum(axis=1)
    df[("TOTAL", "Contractor_Need")] = df.loc[:, (slice(None), 'Contractor_Need')].sum(axis=1)

    return df


def build_peak_headcount_by_program_table(results: dict, disciplines_map: dict):
    """
    Creates the table requested: Index=[Year, BuildingType], Columns=Discipline.
    """
    long_data = []

    # 1. Gather all monthly peak data into a single long list
    for label, df_monthly in results.items():
        if df_monthly is None or df_monthly.empty:
            continue

        df = df_monthly.copy()
        df["Year"] = df["Month"].dt.year

        # Group by Year and BuildingType and find the maximum FTE
        summary = (
            df.groupby(["Year", "BuildingType"])["FTE"]
            .max()
            .reset_index()
            .rename(columns={"FTE": label})  # Use the full Discipline name as the value column
        )
        long_data.append(summary)

    if not long_data:
        return pd.DataFrame()

    # 2. Concatenate all data
    long_df = pd.concat(long_data, ignore_index=True)

    # 3. Pivot the table to the desired format: Index=[Year, BuildingType], Columns=Discipline, Values=Peak_FTE
    discipline_cols = [col for col in long_df.columns if col not in ["Year", "BuildingType"]]

    long_df_melted = long_df.melt(
        id_vars=["Year", "BuildingType"],
        value_vars=discipline_cols,
        var_name="Discipline",
        value_name="Peak_FTE"
    )

    # Then pivot back to wide format
    pivot_df = long_df_melted.pivot_table(
        index=["Year", "BuildingType"],
        columns="Discipline",
        values="Peak_FTE"
    ).fillna(0)

    # 4. Add Row Totals
    pivot_df['Program_Total_Peak'] = pivot_df.sum(axis=1)

    return pivot_df


def build_headcount_by_team_table(results: dict, baseline_method, baseline_quantile):
    """
    Creates the table requested: [Year] Index, Columns=[Team, Headcount Metric].
    Teams: SIF (CE), ARS Team, SSD Team, Projects Team (IBIS/AutoStore).
    """
    team_data = {}

    non_ce_disciplines = ["Mechanical Engineer", "Electrical Engineer", "Safety Engineer", "Site Lead"]

    # --- 1. SIF Team (Commissioning Engineer) ---
    df_sif_monthly = results.get("Commissioning Engineer")
    if df_sif_monthly is not None and not df_sif_monthly.empty:
        df_sif_monthly_agg = df_sif_monthly.groupby("Month", as_index=False)["FTE"].sum()
        team_data["SIF (System Integration)"] = build_total_annual_summary(df_sif_monthly_agg, baseline_method,
                                                                           baseline_quantile)

    # --- 2. ARS, SSD, and Projects Teams (Non-CE Trades) ---
    df_non_ce_monthly_list = []
    for label in non_ce_disciplines:
        df = results.get(label)
        if df is not None and not df.empty:
            df_non_ce_monthly_list.append(df.copy())

    if df_non_ce_monthly_list:
        df_combined_monthly = pd.concat(df_non_ce_monthly_list, ignore_index=True)

        def map_to_team(btype):
            if btype == "ARS":
                return "ARS Team"
            elif btype == "SSD":
                return "SSD Team"
            elif btype in ["IBIS", "Autostore"]:
                return "Projects Team (IBIS/Autostore)"
            return None

        df_combined_monthly["Team"] = df_combined_monthly["BuildingType"].apply(map_to_team)

        for team_name in ["ARS Team", "SSD Team", "Projects Team (IBIS/Autostore)"]:
            df_team_monthly = df_combined_monthly[df_combined_monthly["Team"] == team_name]

            if not df_team_monthly.empty:
                df_team_monthly_agg = df_team_monthly.groupby("Month", as_index=False)["FTE"].sum()
                team_summary = build_total_annual_summary(df_team_monthly_agg, baseline_method, baseline_quantile)
                team_data[team_name] = team_summary

    if not team_data:
        return pd.DataFrame()

    final_rows = []
    for team_name, df_summary in team_data.items():
        for year, row in df_summary.iterrows():
            final_rows.append({
                "Year": year,
                "Team": team_name,
                "Internal Headcount": row["Internal_Base"],
                "Contractor Headcount": row["Contractor_Peak"],
                "Total Peak FTE": row["Total_Peak"],
                "Total Headcount": row["Internal_Base"] + row["Contractor_Peak"],
            })

    final_df = pd.DataFrame(final_rows).set_index(["Year", "Team"])
    reshaped_df = final_df.unstack(level='Team').fillna(0)
    reshaped_df.columns = reshaped_df.columns.swaplevel(0, 1)
    reshaped_df = reshaped_df.sort_index(axis=1, level=0)

    return reshaped_df


def build_project_master_list(por: pd.DataFrame, known_dates: pd.DataFrame, scenarios: pd.DataFrame,
                              fallback_scen_name: str):
    """
    Generates a master list of all projects (known and dummy) with assigned Go-Live dates.
    Format: BuildingType | Project_ID | Go-Live Date
    """
    master_list_rows = []

    # Determine the fallback scenario's quarterly shares
    fallback_row = scenarios[scenarios['ScenarioName'] == fallback_scen_name].iloc[0]
    q_shares = {k: fallback_row.get(f"{k}_Share", 0.0) for k in ["Q1", "Q2", "Q3", "Q4"]}

    # Map known dates for quick lookup
    known_starts_map = {}
    if not known_dates.empty:
        for _, kd_row in known_dates.iterrows():
            key = (kd_row['BuildingType'], kd_row['Year'])
            if key not in known_starts_map:
                known_starts_map[key] = []
            known_starts_map[key].append((kd_row['Project_ID'], kd_row['Go-Live Date']))

    # Iterate through the POR
    year_cols = [c for c in por.columns if str(c).isdigit()]

    for _, row in por.iterrows():
        btype = row["BuildingType"]

        for y_str in year_cols:
            y = int(y_str)
            launches = row[y_str]
            if pd.isna(launches) or launches == 0: continue
            launches = int(launches)

            known_launches = known_starts_map.get((btype, y), [])
            num_known = len(known_launches)

            go_live_months = assign_go_live_months_quarter_based(launches, y, q_shares)

            # 1. Known Projects
            for i in range(num_known):
                known_id, go_live_date = known_launches[i]
                master_list_rows.append({
                    'Building Type': btype,
                    'Project ID': known_id,
                    'Go-Live Date': go_live_date.strftime('%Y-%m-%d'),
                    'Source': 'Known Date',
                })

            # 2. Unknown Projects (Fallback)
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue

                gy, gm = go_live_months[i]
                scheduled_go_live_date = pd.Timestamp(year=gy, month=gm, day=1)

                master_list_rows.append({
                    'Building Type': btype,
                    'Project ID': f"{btype}-{y}-{i + 1}",
                    'Go-Live Date': scheduled_go_live_date.strftime('%Y-%m-%d'),
                    'Source': f'Fallback ({fallback_scen_name})',
                })

    final_df = pd.DataFrame(master_list_rows)
    # The output format Building Type | Project ID | Go-Live Date | Source (Optional)
    return final_df[['Building Type', 'Project ID', 'Go-Live Date', 'Source']].sort_values('Go-Live Date')


# ---------------------------------------------------
# STREAMLIT APP MAIN FUNCTION
# ---------------------------------------------------

def main():
    st.title("MHE Integration Staffing Plan")

    # ---------------- Sidebar: Input File ----------------
    st.sidebar.header("1. Input Data Source")
    uploaded_file = st.sidebar.file_uploader(
        "Upload commissioning_input.xlsx",
        type=["xlsx"],
        help="Requires POR, Scenario_Params, KNOWN_DATES, and assumption sheets."
    )

    if uploaded_file is None:
        st.warning("Please upload your input Excel file in the sidebar to begin.")
        return

    # Load data from the uploaded file
    por, scenarios, assumptions_dict, known_dates = load_inputs_from_excel(uploaded_file)

    if por is None or scenarios is None:
        st.error("Error loading core data.")
        return
    if not assumptions_dict:
        st.warning("No valid assumption sheets found.")

    # ---------------- Sidebar: Filters ----------------
    st.sidebar.header("2. Configuration")

    scenario_options = list(scenarios["ScenarioName"].unique())
    if not known_dates.empty:
        scenario_options.insert(0, "HYBRID")

    selected_scen = st.sidebar.selectbox("Select Scenario to Analyze", options=scenario_options)

    fallback_choice = None
    if selected_scen == 'HYBRID':
        st.sidebar.markdown('**Hybrid Fallback:**')
        fallback_choice = st.sidebar.selectbox(
            "Select Fallback Schedule",
            options=list(scenarios["ScenarioName"].unique())
        )
        if known_dates.empty:
            fallback_choice = 'LEVEL_LOAD'
    else:
        fallback_choice = selected_scen

    st.sidebar.header("3. Internal Staffing Strategy")
    baseline_choice = st.sidebar.selectbox("Baseline Headcount Rule", ["Lean (P50)", "Moderate (P70)", "Robust (P90)"],
                                           index=1)
    b_map = {"Lean (P50)": 0.5, "Moderate (P70)": 0.7, "Robust (P90)": 0.9}
    baseline_quantile = b_map[baseline_choice]
    baseline_method = 'quantile'

    # Filter scenarios
    if selected_scen != 'HYBRID':
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == selected_scen].copy()
    else:
        filtered_scenarios = scenarios[scenarios["ScenarioName"] == fallback_choice].copy()

    # ---------------- Processing ----------------
    disciplines_map = {
        "Commissioning Engineer": ("CE", "CE"),
        "Mechanical Engineer": ("ME", "ME"),
        "Electrical Engineer": ("EE", "EE"),
        "Safety Engineer": ("SAFETY", "SAFETY"),
        "Site Lead": ("LEAD", "LEAD"),
    }

    annual_summaries = {}
    results = {}
    for label, (prefix, sheet_key) in disciplines_map.items():
        df_assump = assumptions_dict.get(sheet_key)
        if df_assump is not None:
            monthly_data = build_monthly_labor_detailed(
                por, df_assump, filtered_scenarios, prefix,
                known_dates, fallback_choice, selected_scen
            ).get(selected_scen)

            results[label] = monthly_data

            if monthly_data is not None and not monthly_data.empty:
                annual_summaries[prefix] = build_total_annual_summary(monthly_data, baseline_method, baseline_quantile)

    # ---------------- CONSOLIDATED HIRING DEMAND ----------------
    st.header("Consolidated Hiring Demand")
    if annual_summaries:
        por_summary = build_por_summary(por)
        consolidated_df = build_consolidated_report(por_summary, annual_summaries, selected_scen)
        st.dataframe(consolidated_df.style.format("{:.1f}"), use_container_width=True)

    st.markdown("---")

    # ---------------- PROJECT MASTER LIST ----------------
    st.header("Project Master List (POR Detail)")
    st.caption("Detailed view of all scheduled projects. Known dates are prioritized; others are forecasted.")

    project_master_df = build_project_master_list(por, known_dates, scenarios, fallback_choice)

    if not project_master_df.empty:
        st.dataframe(project_master_df, use_container_width=True)
    else:
        st.info("No projects found in the Plan of Record (POR).")

    st.markdown("---")

    # ---------------- HEADCOUNT BY ORGANIZATIONAL TEAM ----------------
    st.header("Headcount by Organizational Team")
    team_headcount_df = build_headcount_by_team_table(results, baseline_method, baseline_quantile)
    if not team_headcount_df.empty:
        st.dataframe(team_headcount_df.style.format("{:.1f}"), use_container_width=True)

    st.markdown("---")

    # ---------------- PEAK HEADCOUNT BY PROGRAM ----------------
    st.header("Peak Headcount by Program")
    peak_program_df = build_peak_headcount_by_program_table(results, disciplines_map)
    if not peak_program_df.empty:
        st.dataframe(peak_program_df.style.format("{:.1f}"), use_container_width=True)

    st.markdown("---")

    # ---------------- Monthly Demand Charts ----------------
    st.subheader("Monthly Demand Visuals")
    for label, df_monthly in results.items():
        if df_monthly is None or df_monthly.empty: continue
        st.markdown(f"### {label} FTE Demand")

        # Simplified chart logic for display
        chart = alt.Chart(df_monthly).mark_bar().encode(
            x='Month', y='FTE', color='BuildingType', tooltip=['Month', 'BuildingType', 'FTE']
        )
        st.altair_chart(chart, use_container_width=True)

        with st.expander(f"View Data for {label}"):
            st.dataframe(df_monthly.groupby('Month')['FTE'].sum().reset_index(), use_container_width=True)
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