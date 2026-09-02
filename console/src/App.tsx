import './App.css'

export default function App() {
  return (
    <main className="console">
      <h1>Sweep console</h1>
      <p>
        Operator console for the Sweep drone swarm. Map, gesture readout, ledger, video mosaic,
        focus, detections, and health strip arrive here from M1 onward. All state comes from
        the relay.
      </p>
      <p>
        The webcam prototype, <code>swarm-gesture-console.html</code>, is served unchanged from{' '}
        <code>public/phase0/</code> while it is ported into components.
      </p>
    </main>
  )
}
