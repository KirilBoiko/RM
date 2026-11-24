# OneClick Airways (`4L`) Commercial & RM Strategy Playbook

## 1. Inventory Structure: Freesale vs. Tour-Operator Blocks
- All 737-800 flights carry `189` physical Economy seats, split between **Tour-Operator / Charter Blocks** (`50–110` seats on TLV routes) and **Net Sellable Freesale Capacity** (`79–139` seats).
- **Critical Rule:** Always evaluate Load Factor and Remaining Seats against **`Net Sellable Capacity`** (`Commercial_Load_Factor_%`), never total `189` seats.
- **Soft-Block Release Watch (`D-14` to `D-7`):** On flights with high blocked seats (`>= 80`), verify block fill before closing mid-tier RBDs (`N`/`V`) too aggressively outside `D-10`, as returned soft-block seats expand freesale supply suddenly.

## 2. Eliminating Jagged Price & Demand Curves
Historically, manual simultaneous edits to both RBD availability and dollar tariffs created sharp price spikes (choking conversion) followed by steep discounts. Follow a **smooth step-up trajectory**:
1. **`DBD > 30` (Base Building):** Target steady early base load (`20–30%` freesale LF) in `U / Q / O` (`$80–$134` Lite) unless group/holiday surge pushes pace `>+15%` above benchmark.
2. **`DBD 15–30` (Core Curve Progression):** Transition smoothly through `Q -> O -> N` (`$115–$153` Lite). Step up **1 RBD tier at a time** as `freesale_lf_pct` crosses benchmark milestones or EMSR-b bid price exceeds the lowest open fare.
3. **`DBD 7–14` (Yield Capture vs. Spoilage Balance):**
   - If **ahead of benchmark** (`pace >= +5%`) or **forecast final LF >= 88%**: close `U/Q/O/N`, hold `V/M/L` (`$180–$235` Lite).
   - If **materially behind benchmark** (`pace <= -20%` and `DBD >= 6`): stimulate demand by opening **at most 1 tier down** (subject to `min_fare_by_dbd`) so `4L` sits `$10–$25` below cheapest nonstop competitor (`Arkia` / `ISRAIR` / `EL AL`).
4. **`DBD 0–5` (Inelastic Late Window):** Never cut fares inside `D-5`. Late VFR/business flyers on `TBS-TLV` and `BUS-TLV` are schedule-driven; protect `V/M/L/T+` (`>= $180` Lite).

## 3. Branded Fare Discipline (`Click Lite` vs. `ClickStand`)
- Maintain a clean, predictable **`+$50` spread** from `Click Lite` (hand baggage) to `ClickStand` (checked bag + seat/flexibility) so ancillary baggage pricing (`Luggage_revenue`) is never cannibalized by underpriced `ClickStand` fares.
