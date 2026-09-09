import type { NewSpace } from '../atlas/types'

/** Editorial starters, never injected into the live directory or reported as incidents. */
export interface CommunityExample {
  id: string
  theme: 'creek' | 'mural' | 'park'
  space: NewSpace
  purpose: string
  invitation: string
  views: readonly string[]
}
export const COMMUNITY_EXAMPLES: readonly CommunityExample[] = [
  {
    id: 'shoal-creek', theme: 'creek',
    space: { title: 'A little love for Shoal Creek', place: 'Shoal Creek Trail · Austin, Texas',
      category: 'community', latitude: 30.2826, longitude: -97.7489, radius: 100,
      description: 'Let’s build a shared picture of our favorite stretch of Shoal Creek. Photos of the trail, creek banks, and public crossings can help neighbors understand the space and plan a thoughtful cleanup. This is a starter idea, not a report of a current hazard.' },
    purpose: 'Help neighbors see the trail from every angle.',
    invitation: 'Walking the public trail? A few overlapping photos are a lovely place to start. No special gear needed.',
    views: ['A wide view from the public path', 'The same crossing from both approaches', 'A short, steady video with surrounding detail'],
  },
  {
    id: 'east-austin', theme: 'mural',
    space: { title: 'Color, stories & East Austin', place: 'East César Chávez · Austin, Texas',
      category: 'community', latitude: 30.2588, longitude: -97.7245, radius: 80,
      description: 'A neighborhood story told through public art and the streets around it. Together, we can document a streetscape from the sidewalk, credit the artists we know, and contribute overlapping views for a shared 3D reconstruction. Ask permission before photographing people or entering private property.' },
    purpose: 'Celebrate a streetscape, one perspective at a time.',
    invitation: 'Bring your curiosity. Share a wider view of the block, then a closer view of the details that made you stop.',
    views: ['The streetscape from a public sidewalk', 'Overlapping left, center, and right views', 'A caption with artist credit, if known'],
  },
  {
    id: 'mueller', theme: 'park',
    space: { title: 'A park for every neighbor', place: 'Mueller Lake Park · Austin, Texas',
      category: 'survey', latitude: 30.2973, longitude: -97.7054, radius: 120,
      description: 'Help build a clearer picture of Mueller Lake Park’s public paths, entrances, and resting spots. Shared photos can make it easier to understand the layout before a visit. Describe what you can observe; photos alone do not certify accessibility or safety.' },
    purpose: 'Make a first visit feel a little more familiar.',
    invitation: 'Start with a public entrance or path junction. A calm, well-lit view helps someone else understand how the paths connect.',
    views: ['A public entrance with nearby landmarks', 'A junction photographed in both directions', 'Resting spots and the paths leading to them'],
  },
]
