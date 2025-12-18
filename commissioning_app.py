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
            # RENAME COLUMN IF USER KEPT 'Start_Date' by mistake
            if 'Start_Date' in known_dates.columns and 'Go-Live Date' not in known_dates.columns:
                known_dates = known_dates.rename(columns={'Start_Date': 'Go-Live Date'})

            known_dates['Go-Live Date'] = pd.to_datetime(known_dates['Go-Live Date'])

            # Ensure required columns exist
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

    # Load Discipline Assumption Sheets (Updated for SiteOps)
    assumptions_dict = {}
    sheets_to_load = {
        "CE": "CE_Assumptions",
        "ME": "ME_Assumptions",
        "EE": "EE_Assumptions",
        "SITEOPS": "SITEOPS_Assumptions",  # Renamed from SAFETY
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
        # Fallback logic if specific column is missing (try generic or skip)
        return {}

    monthly_by_scen = {}  # Initialize dictionary

    scen_to_use = selected_scen_name if selected_scen_name != 'HYBRID' else fallback_scen_name
    scenario_row = scenarios[scenarios['ScenarioName'] == scen_to_use].iloc[0]
    q_shares = {k: scenario_row.get(f"{k}_Share", 0.0) for k in ["Q1", "Q2", "Q3", "Q4"]}

    rows = []

    # Map known dates
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

            go_live_months = assign_go_live_months_quarter_based(launches, int(y), q_shares)

            project_details = []

            known_launches = known_dates_map.get((btype, int(y)), [])
            num_known = len(known_launches)

            # 1. Known Projects
            for i in range(num_known):
                known_id, go_live_date = known_launches[i]
                project_details.append({
                    'Go_Live_Date': go_live_date,
                    'Project_ID': known_id
                })

            # 2. Unknown Projects
            for i in range(num_known, launches):
                if i >= len(go_live_months): continue

                gy, gm = go_live_months[i]
                go_live_date = pd.Timestamp(year=gy, month=gm, day=1)
                dummy_id = f"{btype}-{y}-{i + 1}"

                project_details.append({
                    'Go_Live_Date': go_live_date,
                    'Project_ID': dummy_id
                })

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
            df_agg = (
                df.groupby(["Month", "BuildingType", "Scenario"], as_index=False)
                .agg({"FTE": "sum", "Active_Projects": "sum"})
                .sort_values("Month")
            )
        else:
            df_agg = pd.DataFrame(columns=["Month", "BuildingType", "F