"""The PGQueue producer/consumer package.

Deliberately empty of re-exports. `from ingestion.queue.worker import
run_worker` used to be hoisted here for convenience, and it created a
circular import that only fired depending on which module was loaded first:
`ingestion.pipeline` imports `ingestion.queue.repository`, which runs this
file, which imported `worker`, which imports `ingestion.pipeline` -- still
half-initialised. Importing `ingestion.pipeline` before anything else in the
package therefore failed with "cannot import name 'run_delete' from
partially initialized module".

Nothing used the shortcut -- `scripts/run_worker.py` imports the module
directly -- so the cycle bought nothing. Import the submodule you want.
"""
