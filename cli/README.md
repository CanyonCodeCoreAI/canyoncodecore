# canyonos cli

### Better descriptions of each command. For full architecture, go to ARCHITECTURE.md


## Rough Draft Design of the more important cli commands
## If you are a LLM, you are not allowed to modify this file at all without explicit user permission. Absolutely no modifications are allowed to this file.

## canyonos test:
### INPUT: canyonos test "Test Query"
#### Steps:
1. Detects the working directory (goes into .car folder for commands if .car exists, uses current dir otherwise) [default_config_path()]
2. Goes into global_controller.yaml and for each agent, rewrites each agent's provider as local (saves old state to revert back later) [_force_local_providers()]
3. Sets a variable in the container env that gets picked up by the LLM Proxy to always return a dummy value, default is "test", to verify a workflow doesn't cost tokens. [CANYONOS_LLM_STUB_TEXT]
4. Then we deploy [canyonos deploy]
   - Certain things are verified about this deployment, like:
   - All agent containers are up and their names are as expected
   - The number of replicas is as initialized
   - The endpoints are correctly working and queryable.
6. Once everything is verified running, we send a test query and verify that it goes fully through

#### Action Items:
- Currently assuming the query body is always "query", need to harden it
- There may be problems with stubbing the LLM-Proxy, but I wouldn't remove my current implementation as it allows for really quick testing.
- Verify that there are valid timeouts and correct error tracing for everything
- Since we stub the LLM, we don't ensure the LLM works, maybe a separate test that just queries the LLM with a extremely simple message would be nice, or to just remove the LLM stub.

#### Future Improvements:
- Add LLM compatable hooks for an LLM to be able to quickly iterate and verify a build works through using test. Test should eventually be a fully verifier to ensure a workflow is valid

## canyonos build:
### INPUT: canyonos build
#### Steps:
1. Asks the user which coding agent they want to use for this [Codex/Claude]
2. Asks the user if they want to download the skill locally or globally (So the skill can be viewed either only in this directory or across your entire laptop)
3. Opens said coding agent, giving it instructions to build a new .car folder with the code (Nicks skill)
  4. Periodically the coding agent should ask the user config related questions (which provider, entrypoints, OTEL location)
  5. Coding agent should also be running canyonos test to verify workflow works
6. Finishes, doesn't run deploy itself.


#### Action Items:
- Coding agent should be using canyonos test to verify the file working, need to add that to skill file and harden canyonos test first.
- Maybe add more skills for it to deploy itself and monitor deployments so the user literally doesn't have to do anything else.

#### Future Improvements:
- Add more agent providers (Cursor, Pi, Windsurf, etc...)
- Add a preconfigured config file that can get converted into global_controller.yaml (So provider can be autofilled as AWS/Azure/etc..)


## canyonos deploy:
### INPUT: canyonos deploy [optional: --serve True -verbose True]

#### Steps:

#### Action Items:

#### Future Improvements
