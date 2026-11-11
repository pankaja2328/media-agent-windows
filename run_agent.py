from media_agent.agent import ask_agent, build_agent


def main():
    print("Media Agent is ready. Type exit or quit to close.\n")
    agent = build_agent()
    while True:
        try:
            prompt = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit", "/exit", "/quit"}:
            print("Goodbye.")
            break
        print("\nWorking...\n")
        try:
            print(f"Agent: {ask_agent(agent, prompt)}\n")
        except KeyboardInterrupt:
            print("\nRequest cancelled. You can enter another prompt.\n")
        except Exception as error:
            print(f"Agent error: {error}\nYou can retry or enter a different prompt.\n")


if __name__ == "__main__":
    main()

