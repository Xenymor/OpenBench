# OpenBench

OpenBench is an open-source Chess Engine Testing Framework for UCI engines. OpenBench provides a lightweight interface and client to facilitate running fixed-game tests as well as SPRT tests to benchmark changes to engines for performance and stability. OpenBench supports [Fischer Random Chess](https://en.wikipedia.org/wiki/Chess960).

OpenBench is the primary testing framework used for the development of [Ethereal.](https://github.com/AndyGrant/Ethereal) The primary instance of OpenBench can be found at [http://chess.grantnet.us](http://chess.grantnet.us/). The Primary instance of OpenBench supports development for
[Berserk](https://github.com/jhonnold/berserk), [Bit-Genie](https://github.com/Aryan1508/Bit-Genie), [BlackMarlin](https://github.com/dsekercioglu/blackmarlin), [Demolito](https://github.com/lucasart/Demolito), [Drofa](https://github.com/justNo4b/Drofa), [Ethereal](https://github.com/AndyGrant/Ethereal), [FabChess](https://github.com/fabianvdW/FabChess), [Halogen](https://github.com/KierenP/Halogen), [Igel](https://github.com/vshcherbyna/igel), [Koivisto](https://github.com/Luecx/Koivisto), [Laser](https://github.com/jeffreyan11/laser-chess-engine), [RubiChess](https://github.com/Matthies/RubiChess), [Seer](https://github.com/connormcmonigle/seer-nnue), [Stash](https://github.com/mhouppin/stash-bot), [Weiss](https://github.com/TerjeKir/weiss), [Winter](https://github.com/rosenthj/Winter), and [Zahak](https://github.com/amanjpro/zahak). A dozen or more engines are using their own private, local instances of OpenBench.

You can join OpenBench's [Discord server](https://discord.com/invite/9MVg7fBTpM) to join the discussion, see what developers are working on and talking about, or to find out how you can contribute to the project and become a part of it. OpenBench is heavily inspired by [Fishtest](https://github.com/glinscott/fishtest). The project is powered by the [Django Web Framework](https://www.djangoproject.com/) and [fastchess](https://github.com/Disservin/fastchess).

Documentation for OpenBench is available in the [Wiki](https://github.com/AndyGrant/OpenBench/wiki)

## Updating this server

After pulling new changes, run these in the server's virtual environment (`source <venv>/bin/activate`), in this order:

```bash
git pull
python manage.py migrate
python manage.py collectstatic --noinput
```

`collectstatic` copies the files into the `STATIC_ROOT` of the server's `OpenSite/settings.py`, the folder that the web server serves `/static/` from. The repository does not set one, so it must be set there.

Only restart the server after `collectstatic`, otherwise it keeps serving the old JavaScript and CSS. Whenever a static file changes, also raise `OPENBENCH_STATIC_VERSION` in `OpenBench/config.py`, so that browsers load the new files instead of their cached copies.

## Automation on this server

These additions are configured by files in `Config/`, which are never committed:

| File | Purpose |
|------|---------|
| `credentials.discord` | `webhook=` and `site=` for Discord messages |
| `credentials.webhook` | Secret of the Github webhook, in its first line |
| `credentials.<engine>` | Github token. MERGE and COMMIT Nodes need one that may push and open pull requests |

- **Push-to-Test**: Under *Templates*, a Workflow can be saved as a Template, with `$branch` in place of a branch. A pushed commit whose message contains `[ob]` starts the Template `default` for that branch, `[ob:name]` the Template of that name. Add the webhook to the repository on Github (Settings, Webhooks): Payload URL `https://<site>/api/github/push/`, content type `application/json`, the secret from `Config/credentials.webhook`, and only the push event.
- **MERGE and COMMIT Nodes**: A MERGE brings a tested engine into a branch, as a pull request, a merge commit or a fast-forward. A COMMIT writes the values an SPSA tuned into a source file, found by a pattern such as `\b{name}\b\s*=\s*(-?\d+(?:\.\d+)?)`.
- **Error limits**: Tests are stopped after too many crashes, illegal moves or time losses. The limits can be changed in `Config/config.json`, for example `"error_limits" : { "crashes" : 3, "illegals" : 1, "timeloss_percent" : 1.0, "timeloss_min_games" : 400 }`. A limit of 0 disables it.
- **Checks of new Tests**: Warnings for a Dev with the Bench of its Base, a Dev behind its Base, and a Base branch with newer commits. SPRT Nodes can switch to other bounds (*Same Bench Bounds*) when the Benches match.
- **ETAs** for running Workloads and for the Nodes of Workflows.
- **SPSA**: The history of every parameter on the tune's page, and an optional *Auto-Stop* once no parameter moves by more than the given number of C_end.
- **Ladders**: Under *Ladders*, every new commit of a branch plays a fixed number of games against one anchor commit.
- **Journal**: Tags and notes on every Test, searchable, and a *Changelog* of the passed SPRTs since a date or a tag.
