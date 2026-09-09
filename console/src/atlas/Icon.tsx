import type { CSSProperties } from 'react'

const paths = {
  spaces: 'M12 3 3 8l9 5 9-5-9-5ZM3 12l9 5 9-5M3 16l9 5 9-5',
  pin: 'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0ZM15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  plus: 'M12 5v14M5 12h14',
  close: 'm6 6 12 12M6 18 18 6',
  arrow: 'M5 12h14m-6-6 6 6-6 6',
  back: 'M19 12H5m6-6-6 6 6 6',
  camera: 'M3 7h4l2-3h6l2 3h4v13H3V7Zm13 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  video: 'M3 5h12v14H3V5Zm12 6 6-4v10l-6-4',
  panorama: 'M3 5c6 3 12 3 18 0v14c-6-3-12-3-18 0V5Zm0 10 5-5 5 5 4-3 4 3',
  people: 'M16 21v-3a5 5 0 0 0-10 0v3M15 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM18 13a5 5 0 0 1 4 5v3',
  target:
    'M12 2v4m0 12v4M2 12h4m12 0h4M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0ZM14 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z',
  grid: 'M3 3h7v7H3V3Zm11 0h7v7h-7V3ZM3 14h7v7H3v-7Zm11 0h7v7h-7v-7Z',
  cube: 'm12 2 9 5v10l-9 5-9-5V7l9-5Zm0 10 9-5m-9 5L3 7m9 5v10',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5',
  check: 'm5 12 4 4L19 6',
  bookmark: 'M6 3h12v18l-6-4-6 4V3Z',
  spark: 'm12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z',
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 7v5l3 2',
  share:
    'M9 12 15 6M9 12l6 6M9 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM21 4a3 3 0 1 1-6 0 3 3 0 0 1 6 0ZM21 20a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  control: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M12 7v10m-5-5h10',
  live: 'M3 5h18v14H3V5Zm7 4 5 3-5 3V9Z',
  speech:
    'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3ZM5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8',
  gesture:
    'M8 12V4a2 2 0 0 1 4 0v7-8a2 2 0 0 1 4 0v8-5a2 2 0 0 1 4 0v9c0 4-2 7-7 7-3 0-4-2-6-4l-4-5a2 2 0 0 1 3-2l2 1Z',
  devices: 'M5 4h14v11H5V4Zm3 16h8m-4-5v5',
  warning: 'm12 3 10 18H2L12 3Zm0 6v5m0 3v1',
}
export type IconName = keyof typeof paths
export function Icon({
  name,
  size = 20,
  style,
}: {
  name: IconName
  size?: number
  style?: CSSProperties
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  )
}
