# Use one private GitHub App per Poppy Instance

Each Poppy Instance connects to exactly one private GitHub App through GitHub's App Manifest flow and stores the resulting credentials locally. This preserves the Operator-owned trust boundary without a shared Poppy service while retaining GitHub's native approval and repository-selection experience; the trade-off is that every instance needs its own stable public HTTPS endpoint.

A failed or interrupted manifest-code exchange has an ambiguous provider-side outcome: GitHub may already have registered an App. Poppy preserves that attempt's proposed name and owner account, warns the Operator to delete any unused App manually, and gives a retry a fresh proposed name. Such an unverified registration is not the Poppy Instance's connected App.
