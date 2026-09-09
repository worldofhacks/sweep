import { Icon } from '../atlas/Icon'
import { COMMUNITY_EXAMPLES, type CommunityExample } from './examples'
import { journeyScore, type Journey } from './journey'

export function CommunityGuide() {
  return <div className="community-guide">
    <p className="community-lead">You don’t need to be an expert. Just bring your perspective.</p>
    <ol>{[
      ['Find your place', 'Open a space you care about, or create one with a location and a clear purpose.'],
      ['Add what you see', 'Share a photo, video, or 360 scan. Overlapping photos from different positions help build a 3D view.'],
      ['Make the picture better, together', 'Invite someone with another angle. Use requested views to see which perspectives would help next.'],
    ].map(([title, text], index) => <li key={title}><span>{index + 1}</span><div><h3>{title}</h3><p>{text}</p></div></li>)}</ol>
    <div className="community-care"><Icon name="heart" /><p>People first, always. Stay on public paths, ask before photographing people, and never approach a dangerous situation for a capture. For an emergency, contact local emergency services.</p></div>
    <p className="atlas-fine">A community report is an observation, not a verified fact. 3D reconstruction depends on image quality and overlap; a capture does not guarantee a complete model.</p>
  </div>
}

export function JourneyPanel({ journey }: { journey: Journey }) {
  const score = journeyScore(journey)
  const next = score.count < 1 ? 1 : score.count < 3 ? 3 : score.count < 10 ? 10 : null
  return <div className="community-journey">
    <div className="community-welcome-mark"><Icon name="spark" size={32} /></div>
    <span className="atlas-eyebrow">YOUR CONTRIBUTION JOURNEY</span><h3>{score.badge}</h3>
    <p>Little contributions. A clearer world.</p>
    <div className="community-score"><strong>{score.points}</strong><span>perspective points</span></div>
    {next && <><progress aria-label="Progress toward the next contribution badge" value={score.count} max={next} /><p>{score.count} of {next} original contributions toward your next badge.</p></>}
    <div className="community-badges">{[['First perspective', 1], ['Perspective partner', 3], ['Picture builder', 10]].map(([label, count]) => <div key={label} data-earned={score.count >= Number(count)}><Icon name="spark" /><strong>{label}</strong><small>{count} {Number(count) === 1 ? 'original' : 'originals'}</small></div>)}</div>
    <p className="atlas-fine">10 points per original confirmed by the workspace, plus 5 when it answers a capture request. Repeated originals don’t count twice. No points for reporting incidents, sharing GPS, or taking risks.</p>
    <p className="atlas-fine">Private to this device and workspace. Progress is recorded when you open a space containing your contributions. Points have no monetary value, are not verified reputation, and don’t sync to your account yet.</p>
  </div>
}

export function ExampleArt({ theme }: { theme: CommunityExample['theme'] }) {
  return <svg className={`community-example-art ${theme}`} viewBox="0 0 480 160" fill="none" aria-hidden="true">
    <rect width="480" height="160" fill="var(--color-accent-soft)" />
    {theme === 'creek' ? <><path d="M-20 140C90 10 170 190 290 50S420 20 510-30" stroke="#afd8e8" strokeWidth="72" /><path d="M-20 65C100-40 165 90 290-5M40 210C150 70 235 200 365 70S450 80 525 30" stroke="#fffefa" strokeWidth="8" /><path d="m208 42 42 84m-53-79 42 84" stroke="#597f8e" strokeWidth="5" /></> : theme === 'mural' ? <><rect x="70" y="28" width="335" height="130" rx="12" fill="#cadfeb" /><path d="M85 149 152 54l55 80 53-63 62 78" stroke="#7ca9be" strokeWidth="20" /><circle cx="336" cy="65" r="20" fill="#fffefa" /><path d="M50 152h390" stroke="#597f8e" strokeWidth="5" /></> : <><path d="M120 190c-90-130 20-185 95-115s140-80 170 0-80 70-105 130" fill="#cce4d8" /><ellipse cx="300" cy="88" rx="88" ry="40" fill="#9dcee2" /><path d="M0 130c80-100 110 90 210-50s210 60 280-35" stroke="#fffefa" strokeWidth="12" /></>}
    <circle cx="238" cy="80" r="21" fill="var(--color-accent)" /><path d="m229 80 6 6 12-12" stroke="white" strokeWidth="3" strokeLinecap="round" />
  </svg>
}

export function ExampleList({ onOpen, query = '' }: { onOpen: (example: CommunityExample) => void; query?: string }) {
  const matches = COMMUNITY_EXAMPLES.filter(example => `${example.space.title} ${example.space.place} ${example.space.description}`.toLowerCase().includes(query.toLowerCase()))
  return <div className="community-examples">
    {!matches.length && <p role="status">No matching examples. Try a different Austin place or story.</p>}
    {matches.map(example => <button className="community-example" key={example.id} onClick={() => onOpen(example)}>
      <ExampleArt theme={example.theme} /><span className="community-example-body"><small>EXAMPLE · {example.space.category === 'survey' ? 'SHARED SURVEY' : 'NEIGHBORHOOD STORY'}</small><strong>{example.space.title}</strong><span>{example.purpose}</span><span className="community-example-place"><Icon name="pin" size={14} />{example.space.place}<Icon name="arrow" size={16} /></span></span>
    </button>)}
  </div>
}
