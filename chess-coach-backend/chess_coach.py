import chess
from helper_functions import (
    open_engine,
    eval_cp,
    classify_move,
    best_line,
    san_line,
    humanish_reply,
    parse_user_move,
)

def main():
    engine = open_engine()
    board = chess.Board()

    print("✅ LLM-style Chess Coach")
    print("You are White in this demo. Enter moves like 'e4', 'Nf3', or 'e2e4'.")
    print("Type 'hint' to see a suggested line. Type 'quit' to exit.\n")
    print(board, "\n")

    while not board.is_game_over():
        # Do not lowercase here — SAN is case-sensitive (e.g., Nf3)
        u = input("Your move > ").strip()
        if u in ("q", "quit", "exit"):
            break

        if u == "hint":
            lines = best_line(engine, board, plies=4)
            if lines:
                print("💡 Hint:", san_line(board, lines[0][0]))
            else:
                if board.is_checkmate():
                    print("💡 Hint: Checkmate — no moves left!")
                elif board.is_stalemate():
                    print("💡 Hint: Stalemate — it’s a draw.")
                else:
                    print("💡 Hint: No suggestions available.")
            continue


        move = parse_user_move(board, u)
        if not move:
            print("❌ Illegal or unrecognized move. Try 'e4' or 'e2e4'.")
            continue

        # Evaluate before & after from YOUR POV (White in this MVP)
        before_cp = eval_cp(engine, board, chess.WHITE)
        board.push(move)
        after_cp  = eval_cp(engine, board, chess.WHITE)
        delta = after_cp - before_cp  # negative = worse for you

        # Classify and show a short hint (NOT a full solution)
        tag = classify_move(before_cp, after_cp)["label"]
        print(f"📝 Your move is: {tag} (Δ {delta} cp)")
        # Offer a tiny idea from current position
        lines_after = best_line(engine, board, plies=4)
        if lines_after:
            print("Coach idea:", san_line(board, lines_after[0][0]))
        print()

        if board.is_game_over():
            break

        # Coach replies (human-ish)
        reply, plan = humanish_reply(engine, board)
        # Compute SAN before pushing; san() requires legality in current position
        reply_san = board.san(reply)
        board.push(reply)
        print(f"… Coach plays: {reply_san}  — plan: {plan}")
        print(board, "\n")

    print("🏁 Result:", board.result())
    try:
        engine.quit()
    except Exception:
        pass

if __name__ == "__main__":
    main()
