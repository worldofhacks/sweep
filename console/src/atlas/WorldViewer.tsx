import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import type { AtlasClient } from './client'
import { Icon } from './Icon'
import { validateAtlasGlb, type AtlasGeometry } from './glb'
import { assertPhotoTextures, disposeModel, framingDistance } from './model'
import { currentSurfaceRegion, surfaceOverlay } from './surfaceOverlay'
import type { SurfaceFocus, SurfaceRegion } from './types'

interface Props {
  client: AtlasClient
  spaceId: string
  jobId: string
  checksum: string
  focus?: SurfaceFocus | null
}

export default function WorldViewer({ client, spaceId, jobId, checksum, focus }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const reset = useRef<() => void>(() => {})
  const inspect = useRef<(region: SurfaceRegion | null) => void>(() => {})
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [geometry, setGeometry] = useState<AtlasGeometry | null>(null)
  useEffect(() => {
    const element = container.current
    if (!element) return
    // The phone's tabs sit below the canvas; selecting 3D must reveal the new view,
    // not leave it above the scroll position inherited from the detail panel.
    if (window.innerWidth <= 600) element.parentElement?.scrollIntoView({ block: 'start', inline: 'nearest' })
    const abort = new AbortController()
    reset.current = () => {}
    inspect.current = () => {}
    queueMicrotask(() => {
      if (!abort.signal.aborted) { setLoaded(false); setError(''); setGeometry(null) }
    })
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
    } catch {
      queueMicrotask(() =>
        setError('3D viewing needs WebGL. The source captures remain available.'),
      )
      return
    }
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2))
    renderer.setClearColor('#172b26')
    renderer.domElement.setAttribute(
      'aria-label',
      'Image-reconstructed 3D space. Drag to orbit, pinch or scroll to zoom.',
    )
    renderer.domElement.tabIndex = 0
    element.appendChild(renderer.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 10000)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.listenToKeyEvents(renderer.domElement)
    const render = () => renderer.render(scene, camera)
    let frameRadius: number | null = null
    controls.addEventListener('change', render)
    const resize = new ResizeObserver(() => {
      renderer.setSize(element.clientWidth, element.clientHeight)
      const aspect = element.clientWidth / Math.max(1, element.clientHeight)
      if (frameRadius !== null) {
        const factor = framingDistance(frameRadius, aspect, camera.fov) / framingDistance(frameRadius, camera.aspect, camera.fov)
        camera.position.sub(controls.target).multiplyScalar(factor).add(controls.target)
      }
      camera.aspect = aspect
      camera.updateProjectionMatrix()
      render()
    })
    resize.observe(element)
    void (async () => {
      try {
        const data = await client.world(spaceId, jobId, abort.signal)
        const digest = await crypto.subtle.digest('SHA-256', data)
        const actual = Array.from(new Uint8Array(digest), (byte) =>
          byte.toString(16).padStart(2, '0'),
        ).join('')
        if (actual !== checksum) throw new Error('The 3D artifact failed its checksum check.')
        const geometry = validateAtlasGlb(data)
        if (abort.signal.aborted) return
        const gltf = await new GLTFLoader().parseAsync(data, '')
        if (abort.signal.aborted) {
          disposeModel(gltf.scene)
          return
        }
        if (geometry.representation === 'textured_mesh') {
          try { assertPhotoTextures(gltf.scene) }
          catch (error) { disposeModel(gltf.scene); throw error }
        }
        gltf.scene.rotation.x = Math.PI
        gltf.scene.traverse((object) => {
          if (object instanceof THREE.Points) {
            const old = object.material
            object.material = new THREE.PointsMaterial({
              size: 2.5,
              sizeAttenuation: false,
              vertexColors: true,
            })
            ;(Array.isArray(old) ? old : [old]).forEach((material) => material.dispose())
          }
        })
        scene.add(gltf.scene)
        const box = new THREE.Box3().setFromObject(gltf.scene)
        // Frame the main observed area initially. Distant context remains reachable
        // with pan/zoom; no reconstructed points are removed to improve the view.
        const axes: number[][] = [[], [], []]
        const point = new THREE.Vector3()
        gltf.scene.traverse(object => {
          if (!(object instanceof THREE.Points || object instanceof THREE.Mesh)) return
          const positions = object.geometry.getAttribute('position')
          for (let index = 0; index < positions.count; index++) {
            point.fromBufferAttribute(positions, index).applyMatrix4(object.matrixWorld)
            axes[0].push(point.x); axes[1].push(point.y); axes[2].push(point.z)
          }
        })
        axes.forEach(values => values.sort((a, b) => a - b))
        const focus = axes.every(values => values.length) ? new THREE.Box3(
          new THREE.Vector3(...axes.map(values => values[Math.floor(values.length * .1)])),
          new THREE.Vector3(...axes.map(values => values[Math.floor(values.length * .9)])),
        ) : box
        const center = focus.getCenter(new THREE.Vector3())
        const radius = focus.getSize(new THREE.Vector3()).length() / 2
        const wholeRadius = box.getSize(new THREE.Vector3()).length() / 2
        if (!Number.isFinite(radius) || radius <= 0)
          throw new Error('The reconstructed bounds are invalid.')
        frameRadius = radius
        camera.near = Math.max(0.0001, radius / 1000)
        camera.far = wholeRadius * 100
        controls.minDistance = radius / 100
        controls.maxDistance = wholeRadius * 20
        reset.current = () => {
          frameRadius = radius
          controls.target.copy(center)
          camera.position
            .copy(center)
            .add(new THREE.Vector3(.6, .35, 2.6).normalize().multiplyScalar(framingDistance(radius, camera.aspect, camera.fov)))
          camera.updateProjectionMatrix()
          controls.update()
          render()
        }
        reset.current()
        let annotation: THREE.LineSegments | null = null
        inspect.current = region => {
          if (annotation) { scene.remove(annotation); disposeModel(annotation); annotation = null }
          if (!region) { reset.current(); return }
          annotation = surfaceOverlay(region)
          scene.add(annotation)
          const target = new THREE.Vector3(...region.center).applyAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI)
          const direction = camera.position.clone().sub(controls.target).normalize()
          controls.target.copy(target)
          frameRadius = region.radius
          camera.position.copy(target).add(direction.multiplyScalar(framingDistance(region.radius, camera.aspect, camera.fov)))
          camera.updateProjectionMatrix(); controls.update(); render()
          if (window.innerWidth <= 600) element.parentElement?.scrollIntoView({ block: 'start', inline: 'nearest' })
        }
        setGeometry(geometry)
        setLoaded(true)
      } catch (value) {
        if (!abort.signal.aborted)
          setError(value instanceof Error ? value.message : 'The 3D model could not be displayed.')
      }
    })()
    return () => {
      abort.abort()
      inspect.current = () => {}
      resize.disconnect()
      controls.dispose()
      disposeModel(scene)
      renderer.dispose()
      renderer.forceContextLoss()
      renderer.domElement.remove()
    }
  }, [client, spaceId, jobId, checksum])

  useEffect(() => {
    if (loaded) inspect.current(currentSurfaceRegion(focus, jobId, checksum))
  }, [focus, jobId, checksum, loaded])

  return (
    <div className="atlas-world-viewer">
      <div ref={container} className="atlas-world-canvas" />
      <div className="atlas-world-controls">
        <span>
          <Icon name="cube" />
          {currentSurfaceRegion(focus, jobId, checksum)?.label ?? 'Reconstructed perspectives'}
        </span>
        <button
          className="atlas-icon-button"
          aria-label="Reset 3D view"
          onClick={() => reset.current()}
        >
          <Icon name="target" />
        </button>
      </div>
      <p className="atlas-world-help">
        {error ||
          (!loaded
            ? 'Loading verified geometry…'
            : `Drag to orbit · pinch to zoom · ${geometry?.representation === 'textured_mesh' ? 'photo-textured surface' : 'sparse points'}, relative scale`)}
      </p>
    </div>
  )
}
