import type { NavigationPreview } from './client'

export function RoutePreview({ preview }: { preview: NavigationPreview }) {
  const route = preview.plan.navigation.route
  const points = route.routes.flatMap(item => item.waypoints)
  const minX = Math.min(...points.map(point => point.x_m)) - 0.5
  const maxX = Math.max(...points.map(point => point.x_m)) + 0.5
  const minY = Math.min(...points.map(point => point.y_m)) - 0.5
  const maxY = Math.max(...points.map(point => point.y_m)) + 0.5
  const scale = Math.min(440 / (maxX - minX), 170 / (maxY - minY))
  const x = (value: number) => 30 + (value - minX) * scale
  const y = (value: number) => 200 - (value - minY) * scale
  return (
    <div className="ct-route-preview">
      <svg viewBox="0 0 500 230" role="img" aria-label={`Planned routes to ${route.destination_zone_id}`}>
        <text x="16" y="18" fill="currentColor" fontSize="11">Route · top view · meters</text>
        {route.routes.map((item, index) => {
          const end = item.waypoints[item.waypoints.length - 1]
          return <g key={item.drone.drone_id} className={`ct-route-line ct-route-line-${index % 4}`}>
            <polyline points={item.waypoints.map(point => `${x(point.x_m)},${y(point.y_m)}`).join(' ')}
              fill="none" stroke="currentColor" strokeWidth="2" />
            {item.waypoints.map((point, i) => <circle key={i} cx={x(point.x_m)} cy={y(point.y_m)} r={i === 0 ? 5 : 2.5} fill="currentColor" />)}
            <text x={x(end.x_m) + 8} y={y(end.y_m) - 8} fill="currentColor" fontSize="12">D{item.drone.drone_id}</text>
          </g>
        })}
      </svg>
      <p>Flight order: {route.execution_order.map(id => `D${id}`).join(' → ')}. Each aircraft holds at its assigned arrival slot.</p>
      <table>
        <caption>Frozen route endpoints</caption>
        <thead><tr><th>Aircraft</th><th>Arrival slot</th><th>Position (m)</th><th>Waypoints</th></tr></thead>
        <tbody>{route.routes.map(item => {
          const end = item.waypoints[item.waypoints.length - 1]
          return <tr key={item.drone.drone_id}><td>D{item.drone.drone_id}</td><td>{item.arrival_slot.slot_id}</td>
            <td>{end.x_m.toFixed(2)}, {end.y_m.toFixed(2)}, {end.z_m.toFixed(2)}</td><td>{item.waypoints.length}</td></tr>
        })}</tbody>
      </table>
    </div>
  )
}
