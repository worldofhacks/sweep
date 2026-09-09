import { useCallback, useEffect, useState } from 'react'
import { AtlasDialog, ConnectForm, SpacesModule, type NativeCaptureRequest } from './SpacesModule'
import { Icon } from './Icon'
import { CaptureRequestNotice } from './CaptureRequestNotice'
import { NativeAtlasClient, nativeCall, type NativeSession, type NativeUpload } from './native'

export function AndroidAtlas() {
  const [client, setClient] = useState<NativeAtlasClient | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [offline, setOffline] = useState(false)
  const [page, setPage] = useState<'spaces' | 'uploads'>('spaces')
  const [connect, setConnect] = useState(false)
  const [captureTarget, setCaptureTarget] = useState<(NativeCaptureRequest & { session: string }) | null>(null)
  const [openingCapture, setOpeningCapture] = useState(false)
  const [captureError, setCaptureError] = useState('')
  const [uploads, setUploads] = useState<NativeUpload[]>([])
  const showConnect = useCallback(() => setConnect(true), [])
  const refreshUploads = useCallback(async () => {
    try { setUploads(await nativeCall<NativeUpload[]>('getUploads')) }
    catch (error) { setNotice(String(error instanceof Error ? error.message : error)) }
  }, [])
  useEffect(() => {
    let mounted = true
    void Promise.all([nativeCall<NativeSession | null>('getSession'), nativeCall<NativeUpload[]>('getUploads')]).then(([session, initialUploads]) => {
      if (mounted) {
        if (session) setClient(new NativeAtlasClient(session, setOffline))
        setUploads(initialUploads)
      }
    }).catch(error => { if (mounted) setNotice(error.message) }).finally(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [])
  useEffect(() => {
    const timer = setInterval(() => void refreshUploads(), 2500)
    return () => clearInterval(timer)
  }, [refreshUploads])
  useEffect(() => {
    const exit = () => { if (page === 'uploads') setPage('spaces'); else void nativeCall('exit') }
    const back = (event: Event) => {
      if (captureTarget) { event.stopImmediatePropagation(); setCaptureTarget(null) }
      else if (connect) { event.stopImmediatePropagation(); setConnect(false) }
      else if (page === 'uploads') { event.stopImmediatePropagation(); setPage('spaces') }
    }
    window.addEventListener('atlas-back', back, true)
    window.addEventListener('atlas-exit', exit)
    return () => { window.removeEventListener('atlas-exit', exit); window.removeEventListener('atlas-back', back, true) }
  }, [page, connect, captureTarget])
  const capture = useCallback(async (payload: NativeCaptureRequest) => {
    if (!client) throw new Error('Connect your workspace first.')
    setCaptureError('')
    setCaptureTarget({ ...payload, session: client.session.id })
  }, [client])
  const chooseCapture = async (op: 'capture' | 'importMedia') => {
    if (!captureTarget || openingCapture) return
    setOpeningCapture(true)
    setCaptureError('')
    try {
      await nativeCall(op, captureTarget)
      setCaptureTarget(null)
      if (op === 'importMedia') setPage('uploads')
    } catch (error) { setCaptureError(error instanceof Error ? error.message : 'Android could not open this action.') }
    finally { setOpeningCapture(false) }
  }
  const act = async (op: string, id?: string) => {
    try { await nativeCall(op, id ? { id } : {}); await refreshUploads() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'The action could not be completed.') }
  }
  const queued = uploads.filter(item => item.state !== 'saved').length
  return <div className="atlas-native">
    <header className="atlas-native-header"><div><Icon name="spaces" size={24} /><strong>SWEEP<span>ATLAS</span></strong></div>
      <button onClick={showConnect} aria-label="Workspace connection"><Icon name="people" size={18} />{client ? 'Workspace' : 'Connect'}</button></header>
    {offline && <p className="atlas-native-offline" role="status">Offline · Saved spaces, no live locations. Drafts and captures stay on this device.</p>}
    {notice && <div className="atlas-native-notice" role="alert"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="Dismiss message"><Icon name="close" /></button></div>}
    {loading ? <div className="atlas-native-loading">Opening your Atlas…</div> : page === 'spaces'
      ? <SpacesModule key={client?.session.id ?? 'disconnected'} services={client ? { atlas: client } : {}}
        initialSpace={client?.session.space} connectNative={showConnect} captureNative={capture} />
      : <main className="atlas-native-uploads"><span className="atlas-eyebrow">SAVED ON THIS DEVICE</span><h1>Your perspectives.</h1>
        <p>Completed originals stay here until you remove them. A saved badge means the workspace returned the same checksum.</p>
        {!uploads.length && <div className="atlas-native-empty"><Icon name="camera" size={40} /><h2>Start with one view.</h2><p>Open a space and add a capture. Photos, videos, and scan views appear here.</p><button className="atlas-primary" onClick={() => setPage('spaces')}>Explore spaces <Icon name="arrow" /></button></div>}
        {uploads.map(item => <article className="atlas-native-upload" data-state={item.state} key={item.id}>
          <Icon name={item.kind === 'video' ? 'video' : item.kind === 'panorama' ? 'panorama' : 'camera'} size={24} />
          <div><h2>{item.displayName || (item.kind === 'panorama' ? '360 scan view' : item.kind === 'video' ? 'Video' : 'Photo')}<span data-state={item.state}>{item.state === 'saved' ? 'Saved ✓' : item.state === 'queued' ? 'Waiting to upload' : item.state === 'importing' ? 'Copying from device' : item.state}</span></h2>
            <p>Added {new Date(item.createdAt).toLocaleString()} · {item.state === 'importing' ? `${(item.sent / 1024 / 1024).toFixed(1)} MB copied` : `${(item.bytes / 1024 / 1024).toFixed(1)} MB`}</p>
            {item.source === 'import' && <p>Imported · Capture time and location unknown. {item.finalized ? 'Original bytes preserved.' : 'The selected source stays unchanged.'}</p>}
            {item.responseTo && <p>{item.state === 'saved' ? 'Saved and linked to the requested view.' : 'For a requested view · link awaiting upload confirmation.'}</p>}
            {item.state === 'importing' && <progress aria-label="Import in progress" />}
            {item.state === 'uploading' && <progress aria-label="Upload progress" value={item.sent} max={Math.max(1, item.bytes)} />}
            {item.error && <p className="atlas-native-upload-error">{item.error}</p>}
            <div className="atlas-native-upload-actions">{['failed', 'queued'].includes(item.state) && <button onClick={() => void act('retryUpload', item.id)}>{item.source === 'import' && !item.finalized ? 'Retry import' : 'Retry upload'}</button>}
              {!['uploading', 'capturing', 'importing'].includes(item.state) && <button onClick={() => void act('exportUpload', item.id)}>{item.finalized ? 'Export original' : 'Export local file'}</button>}
              {!['uploading', 'capturing', 'importing'].includes(item.state) && <button onClick={() => void act('removeUpload', item.id)}>Remove local copy</button>}</div>
          </div></article>)}
      </main>}
    <nav className="atlas-native-nav" aria-label="Atlas navigation">
      <button aria-current={page === 'spaces' ? 'page' : undefined} onClick={() => setPage('spaces')}><Icon name="spaces" /><span>Spaces</span></button>
      <button aria-current={page === 'uploads' ? 'page' : undefined} onClick={() => setPage('uploads')}><Icon name="upload" /><span>Uploads {queued > 0 && <b>{queued}</b>}</span></button>
      <button onClick={() => void act('openFleet')}><Icon name="control" /><span>Fleet</span></button>
    </nav>
    {captureTarget && <AtlasDialog title="Add your perspective" onClose={() => setCaptureTarget(null)}>
      <p className="atlas-native-capture-place">{captureTarget.title}</p>
      {captureTarget.request && <CaptureRequestNotice request={captureTarget.request} />}
      <div className="atlas-native-capture-choices">
        <button className="atlas-native-capture-choice" disabled={openingCapture} onClick={() => void chooseCapture('capture')}>
          <Icon name="camera" size={28} /><span><strong>Use your camera</strong><small>Photo, video, or guided 360 scan. GPS is optional.</small></span><Icon name="arrow" />
        </button>
        <button className="atlas-native-capture-choice" disabled={openingCapture} onClick={() => void chooseCapture('importMedia')}>
          <Icon name="upload" size={28} /><span><strong>Import from device</strong><small>Up to 10 photos or videos, copied unchanged before upload.</small></span><Icon name="arrow" />
        </button>
      </div>
      <p className="atlas-fine">JPEG, PNG, WebP, MP4 or WebM · Up to 64 MB per file. Imports do not inherit your current location or the file’s modification date as capture time.</p>
      {openingCapture && <p role="status">Opening Android…</p>}
      {captureError && <p role="alert" className="atlas-inline-notice">{captureError}</p>}
    </AtlasDialog>}
    {connect && <AtlasDialog title="Connect your workspace" onClose={() => setConnect(false)}><ConnectForm
      createClient={async (connection, space) => new NativeAtlasClient(await nativeCall<NativeSession>('saveSession', { ...connection, space: space ?? null }), setOffline)}
      onConnect={value => { setClient(value as NativeAtlasClient); setOffline(false); setConnect(false); setPage('spaces') }} /></AtlasDialog>}
  </div>
}
