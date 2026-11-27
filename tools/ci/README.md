# tools/ci

What CI itself runs, beside `tools/ci.py`. None of it is imported by the four
packages, and none of it goes in the repository's venv or on the body.

| File | What it is |
|---|---|
| `requirements.in` | The CI-only tools, each at an exact version: zizmor, mutmut, vulture, coverage, pip-audit, build |
| `requirements.txt` | The lock made from it: every tool and everything a tool needs, 47 pins, each with the hash of every file PyPI holds for that version |
| `zizmor.yml` | zizmor's configuration. A finding the project accepts is listed under `ignore` with its reason |
| `check_workflows.py` | The house rules of `.github/workflows/` and the Dependabot file's wait, three of them zizmor's audits too, standard library only |
| `mutmut-targets.txt` | The functions the `mutmut` job may mutate (RP7). Its own header says how it is read |

## Installing the tools

CI installs the lock by hash, through `python tools/ci.py <job> --install`:

```sh
python -m pip install --require-hashes -r tools/ci/requirements.txt
```

pip refuses any file whose hash the lock does not name. On the laptop the
tools go in the tools venv, which `tools/ci.py` looks in, and never in
`emet-sdk/.venv`:

```sh
python -m venv tools/.venv
tools/.venv/Scripts/python.exe -m pip install --require-hashes -r tools/ci/requirements.txt
```

On Linux the interpreter is `tools/.venv/bin/python`.

`tools/ci.py` finds zizmor, build and pip-audit in the tools venv, and
`tools/check_health.py` finds vulture there. coverage and mutmut run inside
the interpreter that runs the suites, so
`tools/ci.py test` and `mutmut` find them only when the lock is installed
there, which is CI's case; on the laptop those two steps are not run.

## Making the lock again

The lock is universal: one file for Linux x86_64 and aarch64, Windows, and
CPython 3.11 to 3.13, with markers where a pin holds on some of them
(colorama on Windows, PyYAML below 3.13, pyyaml-ft on 3.13). uv writes it.
Install uv in a scratch venv outside the repository, and run it from this
folder:

```sh
python -m venv <scratch>/uv
<scratch>/uv/Scripts/python.exe -m pip install uv
cd tools/ci
<scratch>/uv/Scripts/uv.exe pip compile --universal --python-version 3.11 --generate-hashes \
    --exclude-newer <seven days ago>T00:00:00Z --output-file requirements.txt requirements.in
```

`--exclude-newer` keeps out any file uploaded in the last seven days, the
wait `.github/dependabot.yml` and `requirements.in` keep. The lock of
2026-11-26 used 2026-11-19, and let PyYAML through to 2026-11-21 with
`--exclude-newer-package pyyaml=2026-11-21T00:00:00Z`, because emet-sdk's
floor is `pyyaml>=6.0.4` and 6.0.4 was uploaded on 2026-11-20. The command
uv ran is the lock's second line.

Dependabot keeps the lock current through its `uv` ecosystem, which runs
`uv pip compile` again with `--universal`, `--generate-hashes` and
`--python-version` read from the file, and without `--exclude-newer`; its
own seven-day cooldown does that job. It writes the lock's header back as
it found it, so the header names the last regeneration made here
(dependabot-core's `uv` file updater, read 2026-11-26). Its `pip`
ecosystem would run pip-compile, which has no universal mode.

Dependabot reads every `.txt` and `.in` file in this folder whose name
holds "requirements", and any other whose every line looks like a
requirement. A new list here needs a line that no requirement could be,
as `mutmut-targets.txt` has.

## Checking a new pin

1. Its licence on PyPI, against the allowed list in `tools/licences.json`,
   which allows MPL-2.0 in a tools venv. With the lock installed in a venv:

   ```sh
   python tools/check_licences.py --requirements tools/ci/requirements.txt --python <python of that venv>
   ```

   On GitHub the `test (py3.13)` job runs the same check after it installs
   the lock, as its advisory step `tools-licences`.

2. Its upload date, seven days old or more.
3. zizmor and `check_workflows.py` over the workflows:

   ```sh
   zizmor --offline --config tools/ci/zizmor.yml .github
   python tools/ci/check_workflows.py .
   ```

The tests of `check_workflows.py` are `tools/tests/test_check_workflows.py`.
