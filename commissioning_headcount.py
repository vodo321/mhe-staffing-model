import sys
from pathlib import Path
import pandas as pd

# ---------------------------------------------
# CONFIG
# ---------------------------------------------

INPUT_FILE = "commissioning_input.xlsx"  # Must be in same folder as this script

# Turn this to True if you want extra debug prints
DEBUG = False


# ---------------------------------------------
# LOAD INPUTS
# ---------------------------------------------

def load_inputs(path: Path):
    """
    Reads:
      - POR: Plan of Record with launches by year and building type
      - CE_Assumptions: CE per launch, duration, lead, efficiency
      - Scenario_Params: quarterly distribution per scenario
    """
    por = pd.read_excel(path, sheet_name="POR")
    ce_assumptions = pd.read_excel(path, sheet_name="CE_Assumptions")
    scenarios = pd.read_excel(path, sheet_name="Scenario_Params")
    return por, ce_assumptions, scenarios


# ---------------------------------------------
# EFFICIENCY MAP
# ---------------------------------------------

def build_efficiency_map(ce_assumptions: pd.DataFrame, years):
    """
    Returns: (BuildingType, Year) -> efficiency factor

    For each building type:
      Baseline_Year uses factor 1.0
      Each year after:
         factor = (1 - Annual_Efficiency_Improvement) ** (Year - Baseline_Year)

    With 20% improvement:
      2026 = 1.0
      2027 = 0.8
      2028 = 0.64
      etc.
    """
    eff = {}
    for _, row in ce_assumptions.iterrows():
        btype = row["BuildingType"]
        baseline_year = int(row["Baseline_Year"])
        annual_impr = float(row["Annual_Efficiency_Improvement"])
        for y in years:
            if y < baseline_year:
                factor = 1.0
            else:
                factor = (1.0 - annual_impr) ** (y - baseline_year)
            eff[(btype, y)] = factor
    return eff


# ---------------------------------------------
# QUARTER-BASED DISTRIBUTION
# ---------------------------------------------

def assign_go_live_months_quarter_based(count, year, q_shares):
    """
    Given total launches in a year and quarterly shares, assign go-live months.

    q_shares: dict like {"Q1": 0.25, "Q2": 0.25, "Q3": 0.25, "Q4": 0.25}

    Quarter mapping:
      Q1 -> months 1-3
      Q2 -> months 4-6
      Q3 -> months 7-9
      Q4 -> months 10-12

    Returns a list of (year, month) tuples for each launch.
    """
    if count <= 0:
        return []

    quarter_months = {
        "Q1": (1, 3),
        "Q2": (4, 6),
        "Q3": (7, 9),
        "Q4": (10, 12),
    }

    quarters = ["Q1", "Q2", "Q3", "Q4"]
    q_counts = {}
    remaining = count

    # First three quarters allocated by rounded share, last quarter gets remainder
    for i, q in enumerate(quarters):
        share = float(q_shares.get(q, 0))
        if i < 3:
            qc = int(round(count * share))
            q_counts[q] = qc
            remaining -= qc
        else:
            q_counts[q] = max(0, remaining)

    # Fix any rounding drift
    diff = count - sum(q_counts.values())
    q_counts["Q4"] += diff

    if DEBUG:
        print(f"[DEBUG] Year {year}: count={count}, q_counts={q_counts}")

    months = []

    for q in quarters:
        q_count = q_counts[q]
        if q_count <= 0:
            continue

        start_m, end_m = quarter_months[q]
        span = end_m - start_m + 1  # 3 months per quarter
        step = span / q_count if q_count > 0 else span

        for k in range(q_count):
            m = start_m + int(k * step)
            if m > end_m:
                m = end_m
            months.append((year, m))

    return months


# ---------------------------------------------
# MONTHLY CE FTE PER SCENARIO
# ---------------------------------------------

def build_monthly_ce_fte(por: pd.DataFrame,
                         ce_assumptions: pd.DataFrame,
                         scenarios: pd.DataFrame):
    """
    For each scenario, computes a monthly CE FTE time series.

    Returns:
      dict: scenario_name -> DataFrame[Month, CE_FTE, Active_Projects]
    """
    # Year columns from POR as strings that look like digits
    year_cols = [c for c in por.columns if str(c).isdigit()]
    years = [int(y) for y in year_cols]

    if DEBUG:
        print(f"[DEBUG] POR year_cols={year_cols}, years={years}")

    if not years:
        print("DEBUG: No year columns detected in POR. Check POR header row.")
        return {name: pd.DataFrame(columns=["Month", "CE_FTE", "Active_Projects"])
                for name in scenarios["ScenarioName"].unique()}

    ce_assumptions_idx = ce_assumptions.set_index("BuildingType")
    eff_map = build_efficiency_map(ce_assumptions, years)

    if DEBUG:
        print("\n[DEBUG] Efficiency map sample:")
        for (b, y), v in sorted(eff_map.items()):
            print(f"  {b} {y}: eff={v:.3f}")
        print()

    monthly_by_scen = {}

    for _, scen in scenarios.iterrows():
        scen_name = scen["ScenarioName"]
        q_shares = {
            "Q1": scen.get("Q1_Share", 0.0),
            "Q2": scen.get("Q2_Share", 0.0),
            "Q3": scen.get("Q3_Share", 0.0),
            "Q4": scen.get("Q4_Share", 0.0),
        }

        rows = []

        for _, row in por.iterrows():
            btype = row["BuildingType"]
            if btype not in ce_assumptions_idx.index:
                continue

            ce_staff = float(ce_assumptions_idx.loc[btype, "CE_Staff_Per_Launch"])
            ce_duration = int(ce_assumptions_idx.loc[btype, "CE_Duration_Months"])
            ce_lead = int(ce_assumptions_idx.loc[btype, "CE_Lead_Months"])

            for y in years:
                launches = row[str(y)]  # column names are strings
                if pd.isna(launches) or launches == 0:
                    continue
                launches = int(launches)

                go_live_months = assign_go_live_months_quarter_based(
                    launches, int(y), q_shares
                )

                if DEBUG:
                    print(f"[DEBUG] Scenario={scen_name}, {btype} {y}: "
                          f"launches={launches}, assigned={len(go_live_months)} go-live months")

                for gy, gm in go_live_months:
                    go_live_date = pd.Timestamp(year=gy, month=gm, day=1)
                    start_date = go_live_date - pd.DateOffset(months=ce_lead)
                    end_date = start_date + pd.DateOffset(months=ce_duration - 1)

                    months = pd.date_range(start=start_date, end=end_date, freq="MS")

                    if DEBUG:
                        print(f"[DEBUG] Launch {btype} go-live {gy}-{gm}: "
                              f"CE footprint months = {len(months)}")

                    for dt in months:
                        yr = dt.year
                        eff = eff_map.get((btype, yr), 1.0)
                        fte = ce_staff * eff

                        # Active_Projects=1 per launch per month; sum => concurrent projects
                        rows.append({
                            "Scenario": scen_name,
                            "Month": dt,
                            "BuildingType": btype,
                            "CE_FTE": fte,
                            "Active_Projects": 1,
                        })

        if rows:
            df = pd.DataFrame(rows)
            df_agg = (
                df.groupby("Month", as_index=False)
                  .agg({"CE_FTE": "sum", "Active_Projects": "sum"})
                  .sort_values("Month")
            )
        else:
            df_agg = pd.DataFrame(columns=["Month", "CE_FTE", "Active_Projects"])

        monthly_by_scen[scen_name] = df_agg

    return monthly_by_scen


# ---------------------------------------------
# ANNUAL SUMMARY (HEADCOUNT + CONTRACTORS)
# ---------------------------------------------

def build_annual_summary(monthly_dict: dict) -> pd.DataFrame:
    """
    Aggregates monthly CE FTE into annual stats per scenario.
    Uses Avg_Monthly_FTE as internal baseline.
    Contractor_FTE = max(Peak - Avg, 0).
    """
    rows = []

    for scen_name, df in monthly_dict.items():
        if df.empty:
            continue

        df2 = df.copy()
        df2["Year"] = df2["Month"].dt.year

        grouped = df2.groupby("Year")["CE_FTE"]

        for year, series in grouped:
            total_fte_months = series.sum()
            avg_fte = series.mean()
            peak_fte = series.max()

            contractor_fte = max(peak_fte - avg_fte, 0.0)

            rows.append({
                "Scenario": scen_name,
                "Year": int(year),
                "Total_FTE_Months": total_fte_months,
                "Avg_Monthly_FTE": avg_fte,
                "Peak_Monthly_FTE": peak_fte,
                "Headcount_Internal_Avg": avg_fte,   # internal baseline
                "Headcount_Contractor_Peak": contractor_fte,  # spike above internal
            })

    if not rows:
        return pd.DataFrame(
            columns=[
                "Scenario", "Year", "Total_FTE_Months",
                "Avg_Monthly_FTE", "Peak_Monthly_FTE",
                "Headcount_Internal_Avg", "Headcount_Contractor_Peak",
            ]
        )

    return pd.DataFrame(rows).sort_values(["Scenario", "Year"])


# ---------------------------------------------
# QUARTERLY PROJECT SUMMARY
# ---------------------------------------------

def build_quarterly_project_summary(monthly_dict: dict) -> pd.DataFrame:
    """
    For each scenario and year, computes:
      - Active projects per quarter (Q1, Q2, Q3, Q4)
      - The quarter with maximum simultaneous projects
    """
    rows = []

    for scen_name, df in monthly_dict.items():
        if df.empty or "Active_Projects" not in df.columns:
            continue

        df2 = df.copy()
        df2["Year"] = df2["Month"].dt.year
        df2["Quarter"] = df2["Month"].dt.quarter

        for year, df_y in df2.groupby("Year"):
            q1 = df_y[df_y["Quarter"] == 1]["Active_Projects"].max() if not df_y[df_y["Quarter"] == 1].empty else 0
            q2 = df_y[df_y["Quarter"] == 2]["Active_Projects"].max() if not df_y[df_y["Quarter"] == 2].empty else 0
            q3 = df_y[df_y["Quarter"] == 3]["Active_Projects"].max() if not df_y[df_y["Quarter"] == 3].empty else 0
            q4 = df_y[df_y["Quarter"] == 4]["Active_Projects"].max() if not df_y[df_y["Quarter"] == 4].empty else 0

            quarter_values = {
                "Q1": q1,
                "Q2": q2,
                "Q3": q3,
                "Q4": q4,
            }

            max_quarter = max(quarter_values, key=quarter_values.get)
            max_projects = quarter_values[max_quarter]

            rows.append({
                "Scenario": scen_name,
                "Year": int(year),
                "Q1_Active_Projects": q1,
                "Q2_Active_Projects": q2,
                "Q3_Active_Projects": q3,
                "Q4_Active_Projects": q4,
                "Max_Quarter": max_quarter,
                "Max_Projects": int(max_projects),
            })

    if not rows:
        return pd.DataFrame(
            columns=[
                "Scenario", "Year",
                "Q1_Active_Projects", "Q2_Active_Projects",
                "Q3_Active_Projects", "Q4_Active_Projects",
                "Max_Quarter", "Max_Projects"
            ]
        )

    return pd.DataFrame(rows).sort_values(["Scenario", "Year"])


# ---------------------------------------------
# MAIN
# ---------------------------------------------

def main():
    args = sys.argv[1:]
    input_file = args[0] if args else INPUT_FILE

    path = Path(input_file)
    if not path.exists():
        print(f"ERROR: Input file not found: {input_file}")
        sys.exit(1)

    por, ce_assumptions, scenarios = load_inputs(path)
    monthly = build_monthly_ce_fte(por, ce_assumptions, scenarios)
    annual = build_annual_summary(monthly)
    quarterly_projects = build_quarterly_project_summary(monthly)

    print("\n==================== INTERNAL VS CONTRACTOR CE HEADCOUNT ====================")
    if annual.empty:
        print("No data.")
    else:
        for scen in annual["Scenario"].unique():
            print(f"\n--- Scenario: {scen} ---")
            df_s = annual[annual["Scenario"] == scen].copy()
            df_s = df_s[[
                "Scenario", "Year",
                "Avg_Monthly_FTE", "Peak_Monthly_FTE",
                "Headcount_Internal_Avg", "Headcount_Contractor_Peak",
                "Total_FTE_Months",
            ]]
            print(df_s.to_string(index=False))

    print("\n==================== QUARTERLY ACTIVE PROJECTS ====================")
    if quarterly_projects.empty:
        print("No data.")
    else:
        for scen in quarterly_projects["Scenario"].unique():
            print(f"\n--- Scenario: {scen} ---")
            df_s = quarterly_projects[quarterly_projects["Scenario"] == scen].copy()
            print(df_s.to_string(index=False))

    print("\n==================== MONTHLY CE FTE (peak preview) ====================")
    for scen_name, df in monthly.items():
        if df.empty:
            print(f"\n--- Scenario: {scen_name} --- No data.")
            continue
        peak = df["CE_FTE"].max()
        peak_rows = df[df["CE_FTE"] == peak]
        print(f"\n--- Scenario: {scen_name} --- Peak CE_FTE = {peak:.2f}")
        print(peak_rows.to_string(index=False))


if __name__ == "__main__":
    main()
