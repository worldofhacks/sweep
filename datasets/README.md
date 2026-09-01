# datasets

Owners: A records gesture sessions; all three write utterances. Phase 1 onward.

- `gesture/`: recorded webcam sessions from the console recorder with hand-labeled intent timestamps (gesture gold set).
- `utterances/`: the 200-utterance language gold set with gold intent sequences.

Recordings are large. Before committing video, track it with Git LFS:

    git lfs install
    git lfs track "datasets/**/*.mp4" "datasets/**/*.webm"
    git add .gitattributes

LFS is not configured yet; the first person to add a recording sets it up.
