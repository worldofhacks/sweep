import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import html from '../../welcome.html?raw'

describe('public Sweep introduction', () => {
  beforeEach(async () => {
    vi.resetModules()
    document.body.innerHTML = new DOMParser().parseFromString(
      html,
      'text/html',
    ).body.innerHTML
    vi.stubGlobal('fetch', vi.fn())
    await import('./main')
  })
  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
  })

  it('explains the product without starting the console or contacting a provider', () => {
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'A place is more than a picture.',
    )
    expect(fetch).not.toHaveBeenCalled()
    expect(document.querySelector('form')).toBeNull()
    expect(document.querySelector('audio, video, iframe')).toBeNull()
  })

  it('ends at the hero caption with no additional sections or footer', () => {
    expect(document.querySelectorAll('main section')).toHaveLength(1)
    expect(document.querySelector('main')?.textContent?.trim()).toMatch(
      /A little world, held together\.$/,
    )
    expect(document.querySelector('footer')).toBeNull()
    expect(document.querySelector('[data-layer]')).toBeNull()
    expect(document.body).not.toHaveTextContent('Concept illustration')
    expect(document.querySelector('figcaption span')).toBeNull()
  })

  it('provides working document targets and preserves the existing console URL', () => {
    for (const link of document.querySelectorAll<HTMLAnchorElement>(
      'a[href^="#"]',
    )) {
      const hash = link.getAttribute('href')!
      if (hash !== '#')
        expect(document.getElementById(hash.slice(1))).not.toBeNull()
    }
    expect(
      screen.getByRole('link', { name: /Try the workspace/ }),
    ).toHaveAttribute('href', '/')
    expect(
      screen.getByRole('link', { name: /Open workspace/ }),
    ).toHaveAttribute('href', '/')
    expect(document.querySelectorAll('h1')).toHaveLength(1)
  })
})
