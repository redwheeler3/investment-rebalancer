# Private State Security Hardening Plan

Status: implementation in progress; private-state cutover and history rewrite pending.

This document records the planned hardening of the private state repository. It
is not a description of the current implementation.

## Decision

Keep the public-code/private-state architecture and Git-based synchronization,
but stop treating repository privacy as sufficient protection for live
Questrade refresh tokens. The synchronization design solves a real problem:
Questrade refresh tokens are single-use, and every successful authentication
must persist the replacement token for both local and scheduled runs. The weak
point is storing the current credential as plaintext in Git.

Use SOPS with age encryption so the private state repository contains only
ciphertext for token files and every app-managed file under `data/`. Keep
`config/settings.yaml` plaintext because it is human-authored configuration
that must remain easy to inspect and edit. This preserves the simple rotating-
state workflow without introducing an external secrets service.

## Initial Scope

1. Encrypt every live `tokens/*.json` file with SOPS/age before it is committed.
2. Encrypt every app-managed file under `data/` with SOPS/age before it is
   committed. This includes portfolio history, tactical regime state, and
   sticky FX-target state; users do not need to edit these files directly.
3. Keep `config/settings.yaml` plaintext so it remains directly readable and
   editable.
4. Store the age private key as a GitHub Actions secret for scheduled runs.
5. Store the local age private key outside both repositories, protected by the
   local operating system or a password manager.
6. Decrypt tokens and app-managed data only in memory for the duration of a run,
   persist updates as ciphertext, and commit only encrypted state files.
7. Ensure temporary plaintext is removed on success and failure, and ensure
   token values cannot appear in command output, exception messages, workflow
   logs, artifacts, or caches.
8. Fail the scheduled job on any encrypted-state read/write error, validate all
   managed ciphertext before rotation and commit, preserve any valid replacement
   token already issued if later processing fails, and serialize overlapping
   sync runs.
9. After the encrypted workflow is verified, remove plaintext tokens and
   app-managed data from the private repository's Git history and
   regenerate/re-authorize the Questrade credentials so any previously retained
   token is invalid.
10. While updating the workflow, pin third-party GitHub Actions to reviewed,
   full commit SHAs. This is inexpensive defense-in-depth, not the main purpose
   of the change.

The history rewrite is destructive and affects every clone. Back up the private
state, pause scheduled runs, verify the exact rewrite targets, and coordinate
the cutover before force-pushing rewritten history.

## Decisions Deferred Until Implementation

- Whether encryption/decryption belongs in small wrapper scripts or directly in
  the Python application. Prefer the smaller design that gives local runs and
  GitHub Actions one consistent, failure-safe path.

## Explicitly Outside the Initial Scope

- Replacing Git-based state synchronization with a cloud secrets manager.
- Encrypting `config/settings.yaml`. It is intentionally plaintext because it
  is user-maintained configuration that needs to be inspected and edited
  directly.
- Pinning the public code checkout to a fixed commit. Doing so would require a
  private-workflow update for every public-code release.
- A comprehensive Python dependency-locking project. Exact dependency locks
  may be worthwhile later, but they are not required to correct the plaintext
  credential design.

## Proposed Cutover Sequence

1. Pause the scheduled private-state workflow to prevent concurrent token
   rotation during migration.
2. Add the SOPS/age configuration and implement the local and CI token lifecycle
   using example credentials first.
3. Add the age private key to GitHub Actions secrets and verify that the key is
   exposed only to the step that needs it.
4. Encrypt the current token files and all existing app-managed data, then update the
   private workflow atomically so no scheduled run expects the wrong file
   format. Leave `config/settings.yaml` plaintext.
5. Run a controlled sync and confirm that authentication succeeds, replacement
   tokens and updated state are encrypted before commit, no temporary
   plaintext remains in the workspace, and logs contain no credential material.
6. Verify a local run uses the same encrypted source of truth and safely pushes
   the newly rotated ciphertext.
7. Back up the private repository, rewrite its history to remove plaintext
   tokens and app-managed data, then force-push the verified rewritten history.
8. Revoke or regenerate the Questrade authorization after the rewrite, seed the
   newly issued credentials through the encrypted path, and verify both local
   and scheduled runs again.
9. Re-enable the schedule and replace or reclone any stale local copies of the
   private repository.

## Completion Criteria

- No live Questrade token exists in the public repository.
- No live Questrade token exists as plaintext in the private repository's
  current tree or rewritten history.
- Every app-managed file under `data/` exists only as ciphertext in the private
  repository's current tree and rewritten history.
- `config/settings.yaml` remains plaintext and directly editable.
- GitHub Actions and local runs can both consume and persist rotating tokens.
- Only encrypted token and app-managed data files are committed or pushed.
- Failure at any stage does not leave plaintext behind or lose the latest
  successfully rotated token.
- Encryption failures are fatal, every automated commit passes a complete
  decrypt-validation check, valid rotated tokens survive downstream failures,
  and overlapping sync runs are serialized.
- Workflow logs, errors, caches, and artifacts do not disclose credentials.
- The public documentation describes the state repository as encrypted rather
  than implying that privacy alone is the credential control.

## Portfolio/Recruiter Framing

The intended technical story is: the project uses a public-code/private-state
split to coordinate Questrade's rotating OAuth state, encrypts that state at
rest in Git with SOPS/age, protects app-managed financial state,
keeps human-authored settings directly editable, and limits decryption to local
execution or the credential-refresh workflow. Repository privacy remains an
additional access control, not the encryption mechanism.
