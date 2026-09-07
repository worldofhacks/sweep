import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import { installRecordingCanvas } from '../testing/canvas-context'

// jsdom implements no canvas; every component that draws gets a recording
// context instead, which the map tests read back.
installRecordingCanvas()

afterEach(cleanup)
