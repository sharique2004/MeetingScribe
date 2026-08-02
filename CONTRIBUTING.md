# Contributing to MeetingScribe

Thanks for your interest in MeetingScribe!

## License and copyright

MeetingScribe is licensed under the GNU GPL v3. The maintainer also ships
binaries through channels whose terms are incompatible with the GPL (for
example the Mac App Store), which is possible only while the maintainer holds
the full copyright — the same model used by Blink Shell.

Because of that, contributions are accepted only under a lightweight
Contributor License Agreement: you keep the copyright to your contribution,
and you grant Sharique Khatri a perpetual, irrevocable, worldwide right to
use, modify, and relicense your contribution as part of MeetingScribe,
including under licenses other than the GPL.

By opening a pull request you agree to these terms. Please add a
`Signed-off-by: Your Name <email>` line to your commits (`git commit -s`).

## Practical notes

- Development happens on the `Production` branch; `main` mirrors it.
- Run from source with `bash setup.sh` (macOS) or `setup.bat` (Windows) — see
  the README.
- Never commit anything from `recordings/`, `docs/`, `test/fixtures/`, or
  `.insforge/` — they are gitignored because they contain real meeting data
  or credentials.
