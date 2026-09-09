# Sweep: product direction for discussion

Draft for owner review · September 9, 2026. This is a proposed direction, not a replacement for the MVP plan or issue tracker.

Owner revision: the implemented landing page is now hero-only, ending at “A little world, held together.” The lower sections, footer, concept/location caption, and layer interaction were removed. The hero CTA scrolls to its illustration; workspace links still open the existing console. The broader page description below records the earlier draft, not the current UI.

## One promise

**Keep the places and moments that matter—together.**

Sweep helps people gather captures of the same real place and preserve their different perspectives. The long-term ambition is a revisitable, immersive memory with a traceable connection to its original material. The public headline is “A place is more than a picture.”

## Problem and first customer hypothesis

A shared experience ends up scattered across phones and chat threads. An organizer has to chase submissions, explain missing context, and assemble something others can revisit. A shared album is the baseline competitor: if Sweep does not make this meaningfully easier or richer, immersive visuals alone are not enough.

Start by interviewing Austin community and small-event organizers. They are a proposed first customer, not confirmed customers. Contributors should be able to participate with a phone. Keep fleet control as an advanced capture capability, not the first thing a new visitor has to understand.

Do not try to launch a social network, robotics platform, world model, and consumer memory app as four equally prominent products. One public story; specialized tools underneath it.

## What each audience needs to see

- Everyday participants: “What can I make, who can see it, and how much effort is this?” Show one useful example and a short path to contributing. Avoid early points-chasing, leaderboards, or forced account setup before value is clear.
- Product experts: a specific job, a usable contribution/review/share loop, honest empty states, and evidence people return. Reduce upload and invitation friction before adding more destinations in the sidebar.
- AI engineers: multimodal source provenance, consent boundaries, useful uncertainty, reproducible evaluations, and a modular pipeline. Show where reconstruction ends and inferred context starts. Avoid claiming that weather models measured conditions at a microphone or AI knows a participant’s feelings.
- Investors: a focused initial customer, a plausible payer, repeated usage, delivery cost, and willingness to pay. A possible paid offering is an organizer workspace with storage and controlled sharing. Pricing and demand remain unvalidated.

## Pilot proposal — before expanding scope

Recruit 5–10 organizers for actual gatherings. Ask them to use their current workflow first, then compare it with Sweep. Suggested learning measures (targets to agree, not existing metrics):

1. Time to a first successful contribution; percentage of invitees who finish uploading.
2. Whether the organizer can publish a useful collection without manual rescue.
3. Whether participants revisit or contribute again, and why.
4. Whether an organizer chooses it for a second gathering and will pay.
5. Storage, processing cost, and support time per useful shared collection.

Prioritize source-preserving capture, understandable permissions, dependable invitations, and reliable sharing. Add historical weather and AI context only if they improve the memory rather than decorate it. Do not train on private memories by default or suggest that access controls and retention are finished when they are not.

## Differentiation to test

World Labs documents reconstruction, generation, and simulation of 3D worlds in Marble. Sweep’s proposed distinction is the collaborative workflow and source-grounded story around a real experience, not a claim to have invented a competing foundation model. Existing reconstruction providers could be infrastructure choices, not the entire product identity.

Primary references: [World Labs documentation](https://docs.worldlabs.ai/), [YC: How to Find Product Market Fit](https://www.ycombinator.com/blog/how-to-find-product-market-fit-peter-reinhardt/). The customer choice, positioning, pilot, and business model above are our hypotheses, not conclusions from these sources.

## Landing implementation and review boundary

- Public draft: /welcome.html. The existing console stays at /; native entry remains unchanged.
- Static HTML plus a small progressive-enhancement script; no React, maps, 3D renderer, authentication SDK, tracking, geolocation, or external provider calls on the landing page.
- Three example buttons explain Place / Atmosphere / People. They are an explicitly labeled conceptual walkthrough, not a fake live demo.
- FAQs work without JavaScript. The initial Place explanation and navigation also remain available without JavaScript.
- Web build includes both HTML entries. A production host must serve welcome.html and the bundled assets; making it the root homepage is a separate routing decision.
- No fabricated customer logos, testimonials, statistics, API availability, or production privacy commitments.
- Hero art: original built-in image generation, encoded as WebP (~213 KB); an illustration inspired by Austin, not a capture or map. The existing Sweep icon geometry is reused for the header.
- Unfinished memory-context changes are separate work in the same checkout and are not a claim of completed functionality in this landing-page task. Nothing is merged, pushed, or committed by this task.

## Hero asset provenance

Verification: production web build passed (existing large console-chunk warnings remain), scoped ESLint passed, and 5 focused landing/configuration tests passed. Desktop 1440×1000 and mobile 390×844 were visually inspected. Browser checks confirmed layer switching, keyboard activation, opening FAQs, loaded images, no mobile horizontal overflow, and no landing-page console errors. This is not a full regression certification of the separate unfinished memory work.

Asset: console/public/landing/austin-memory-world.webp

Generated with the built-in image tool, not the CLI. Original retained at /Users/quietguy/.codex/generated_images/01a08511-fb6a-7bf2-8799-1721cdc9a6c3/exec-c4789437-0001-42d8-845d-5ef4ed18df1e.png. WebP is an encoding optimization; the page uses a CSS edge fade. User-provided references informed the artistic direction but were not copied or embedded.

Final generation prompt:

> Use case: stylized-concept. Asset type: wide editorial hero illustration for Sweep landing page. Primary request: an original calm, inviting miniature memory-world inspired by antique natural-history engravings and modern spatial illustration. Scene: a gently floating limestone island holding a winding blue creek, a small Austin Texas neighborhood, live oak trees, a tiny footbridge, a community picnic and distant modest Austin skyline, protected within a large translucent glass sphere, its open lower edge seamlessly meeting the island. Delicate cream clouds drift within the glass. Hand-inked fine crosshatching and softly painted gouache, sophisticated storybook travel illustration, not photorealistic. Wide 3:2 composition, entire sphere and island centered with generous empty warm ivory margins on all sides. Warm ivory background #f8f5ed, ocean blue water, sage trees and small sun-warmed terracotta details. Quiet wonder, human scale, afternoon light. No text, logos, UI, watermark, gears, robots, mountains or copied competitor elements. This is conceptual artwork, not a real reconstruction. Detailed beautiful single visual with clean silhouette and subtle grounding shadow.
