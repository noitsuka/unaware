# runtime

Node.js 24.16.0 (Windows x64) and the `pokemon-showdown` npm package used by the
simulator worker for team validation and the fallback search. Neither is in git:
`start.bat` downloads `node.exe` from nodejs.org and runs `npm ci --omit=optional`
here on the first run. The worker only loads the simulator and the validator; it
never starts a server or logs into an account.
