import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { AtlasClient } from './client'
import { ReconstructionPanel } from './ReconstructionPanel'

const client = new AtlasClient({ baseUrl: 'https://example.test', sessionId: 'test', token: 'test-only' })

test.each(['sparse_point_cloud', 'textured_mesh'] as const)('describes %s without claiming measured or complete surfaces', representation => {
  render(<ReconstructionPanel client={client} spaceId="space" canBuild={false} onChange={() => {}}
    job={{ status: 'ready', source_count: 11, detail: 'Built from photographs', representation,
      registered_views: 11, prepared_views: 11, points: 14992, faces: 98938,
      mean_reprojection_error_px: .226, experimental: representation === 'textured_mesh' }} />)
  expect(screen.getByText('Camera fit · reprojection')).toBeInTheDocument()
  expect(screen.getByText('Relative · not metric')).toBeInTheDocument()
  expect(screen.getByText(/does not establish geographic measurements or prove complete coverage/)).toBeInTheDocument()
  if (representation === 'textured_mesh') {
    expect(screen.getByText('Surface triangles')).toBeInTheDocument()
    expect(screen.getByText('98,938')).toBeInTheDocument()
    expect(screen.getByText(/not a surface-accuracy score/)).toBeInTheDocument()
    expect(screen.getByText(/not production-qualified/)).toBeInTheDocument()
    expect(screen.queryByText('Observed 3D points')).not.toBeInTheDocument()
  } else {
    expect(screen.getByText('Observed 3D points')).toBeInTheDocument()
    expect(screen.getByText('14,992')).toBeInTheDocument()
    expect(screen.queryByText('Surface triangles')).not.toBeInTheDocument()
  }
})
