# Use one private GitHub App per Poppy Instance

Each Poppy Instance registers its own private GitHub App through GitHub's App Manifest flow and stores the resulting credentials locally. This preserves the Operator-owned trust boundary without a shared Poppy service while retaining GitHub's native approval and repository-selection experience; the trade-off is that every instance needs its own stable public HTTPS endpoint.
