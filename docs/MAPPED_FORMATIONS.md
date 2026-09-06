# Mapped formations

`planner.mapped_formations` creates frozen previews for two- and four-aircraft formations in an owner-approved formation volume. The caller supplies the formation permission separately from destination-navigation permission, the full current fleet position set, an accepted navigation artifact, and a layout with explicit altitude offsets.

The planner supports line and column with two aircraft. Four aircraft may use line, column, wedge, or diamond. It enumerates every assignment for the selected fleet, chooses the lowest total three-dimensional travel distance, and breaks ties by sorted slot order. The result contains map, geometry, configuration, roster, selected connection epochs, exact assignments, and `navigation_plan`, which the existing dispatcher can execute sequentially.

Each target aircraft volume must fit inside the configured formation polygon and altitude bounds, remain free in the accepted grid, and stay separated from every other target. The planner routes formation entries through `NavigationPlanner`, which reserves all aircraft positions and completed arrivals. It rejects approaches that cross. A formation request does not take off a grounded aircraft.

Map configuration decides which named zones are formation-enabled. The current fixtures configure lobby and atrium-front only. Kitchen receives no inferred formation permission or fallback behavior. Synthetic fixtures support software tests. Flight release requires deployment evidence.
