import {
  ladderRungs,
  ladderSentence,
  liveServices,
  nodeCells,
  nodeError,
  nodeRecordFor,
} from '../../catalog/derive'
import type { HealthMetric, ServiceRecord } from '../../catalog/types'
import { formatDroneId } from '../../control/state'
import type { RelayAircraftState } from '../../relay/contract'
import { sortedAircraft } from '../../shell/derive'
import '../catalog.css'
import { EmptyModule } from '../shared'
import type { ModuleProps } from '../types'

/**
 * Connectivity and health, rendered under Reference › Health: health metrics,
 * one row per aircraft node, the shared services, and the degradation ladder.
 * Node rows come from relay aircraft state; versions, RTT, rate and storage
 * come from the catalog and read unreported until a node endpoint exists.
 */
export function ConnectivityModule({ controller, catalog, now }: ModuleProps) {
  const { state } = controller
  const { metrics, nodes, services } = catalog.snapshot
  const fleet = sortedAircraft(state.aircraft)
  const currentNow = now()
  const rungs = ladderRungs(state)
  return (
    <div className="cat-swap">
      <section aria-labelledby="con-metrics-title">
        <h3 className="cat-h3" id="con-metrics-title">
          Health metrics
        </h3>
        {metrics === null ? (
          <EmptyModule what="health metrics" />
        ) : metrics.length === 0 ? (
          <p className="cat-line">No health metrics are reported for this session yet.</p>
        ) : (
          <div className="con-metrics">
            {metrics.map((metric) => (
              <MetricTile key={metric.key} metric={metric} />
            ))}
          </div>
        )}
      </section>

      <section className="cat-section" aria-labelledby="con-nodes-title">
        <h3 className="cat-h3" id="con-nodes-title">
          Per-aircraft nodes
        </h3>
        {fleet.length === 0 ? (
          <p className="cat-line">
            No aircraft have joined this session. The relay reports an empty roster.
          </p>
        ) : (
          <div className="con-table-wrap">
            <table className="con-table">
              <caption>
                Every cell answers what is wrong and what to do. Versions cover aircraft firmware,
                controller firmware, phone model and SDK release.
              </caption>
              <tbody>
                {fleet.map((drone) => (
                  <NodeRow
                    key={drone.drone_id}
                    drone={drone}
                    node={nodeRecordFor(nodes, drone.drone_id)}
                    now={currentNow}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="cat-section" aria-labelledby="con-services-title">
        <h3 className="cat-h3" id="con-services-title">
          Shared services
        </h3>
        <ul className="con-services">
          {liveServices(state).map((service) => (
            <ServiceRow key={service.service_id} service={service} />
          ))}
          {services?.map((service) => (
            <ServiceRow key={service.service_id} service={service} />
          ))}
        </ul>
        {services === null && <EmptyModule what="shared-service status" />}
        {services?.length === 0 && (
          <p className="cat-line">No shared services beyond the relay sockets are reported.</p>
        )}
      </section>

      <section className="cat-section" aria-labelledby="con-ladder-title">
        <h3 className="cat-h3" id="con-ladder-title">
          Degradation ladder
        </h3>
        <p className="con-ladder-intro">
          Each rung drops one capability and keeps the ones below it. The last rung is the keyboard
          stop, which is why it travels on its own authenticated connection.
        </p>
        <ul className="con-ladder" aria-label="Degradation ladder rungs">
          {rungs.map((rung) => (
            <li
              key={rung.label}
              className={rung.current ? 'con-rung is-current' : 'con-rung'}
              aria-current={rung.current ? 'true' : undefined}
            >
              {rung.label}
            </li>
          ))}
        </ul>
        <p className="con-ladder-now">{ladderSentence(state, rungs)}</p>
      </section>
    </div>
  )
}

function MetricTile({ metric }: { metric: HealthMetric }) {
  return (
    <div>
      <p className="con-metric-key">{metric.key}</p>
      <p className={`con-metric-value tone-${metric.tone}`}>{metric.value}</p>
      <p className="con-metric-note">{metric.note}</p>
    </div>
  )
}

function NodeRow({
  drone,
  node,
  now,
}: {
  drone: RelayAircraftState
  node: ReturnType<typeof nodeRecordFor>
  now: number
}) {
  const error = nodeError(drone)
  return (
    <tr>
      <th scope="row">{formatDroneId(drone.drone_id)}</th>
      <td>
        <dl className="con-cells">
          {nodeCells(drone, node, now).map((cell) => (
            <div className="con-cell" key={cell.key}>
              <dt className="con-cell-key">{cell.key}</dt>
              <dd className={`con-cell-value tone-${cell.tone}`}>{cell.value}</dd>
            </div>
          ))}
        </dl>
        {error && <p className="con-error">{error}</p>}
      </td>
    </tr>
  )
}

function ServiceRow({ service }: { service: ServiceRecord }) {
  return (
    <li className="con-service">
      <span className="con-service-label">{service.label}</span>
      <span className={`con-service-value tone-${service.tone}`}>{service.status}</span>
      <span className="con-service-note">{service.note}</span>
    </li>
  )
}
