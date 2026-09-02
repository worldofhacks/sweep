# datasets

Capability area: Interaction, with team-contributed cases. Milestone: M1 onward.

Any engineer may claim a ready recording or corpus task and owns it through review, integration, and evidence. Changes that encode shared-contract or safety expectations name one change owner and require cross-review.

- `gesture/` will hold recorded webcam sessions from the console recorder with hand-labeled intent timestamps (gesture gold set).
- `utterances/` will hold the 200-utterance language gold set with gold intent sequences.

Recordings are large. Before committing video, track it with Git LFS:

```bash
git lfs install
git lfs track "datasets/**/*.mp4" "datasets/**/*.webm"
git add .gitattributes
```

LFS objects do not follow a second remote automatically. After pushing to GitHub, run `git lfs push --all gitlab` so the GitLab copy has the videos too (the GitLab project needs LFS enabled).

LFS is not configured yet; the first person to add a recording sets it up.
