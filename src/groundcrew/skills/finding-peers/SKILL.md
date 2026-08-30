---
name: finding-peers
description: Resolve a ListAgents peer session to the repo, worktree, and branch it's running in, via groundcrew.
---

# Finding peers with groundcrew

`ListAgents` shows peer session names but not what repo or branch they're on.
`groundcrew` (if installed on this machine) already knows:

    groundcrew sessions --json

Each element is one live session:

    {
      "repo": "/home/user/Projects/example",
      "worktree": "/home/user/Projects/example/.claude/worktrees/bridge-cse_01Ab...",
      "address": "bridge-cse-01ab...-x1",
      "title": null,
      "session_id": "...",
      "pid": 12345,
      "branch": "worktree-bridge-cse_01Ab..."
    }

Match a `ListAgents` peer name against a row's `address` field — **exact
string equality**, no hashing or substring matching. The matching row's
`repo`/`worktree`/`branch` is what that peer is working on.

No match means the peer isn't a groundcrew-supervised session, or groundcrew
isn't installed/running there — `sessions` only lists what it supervises.
