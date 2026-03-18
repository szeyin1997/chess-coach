# ⚙️ Tunables (start with these)

# 🔧 1) UPDATE THIS PATH after installing Stockfish
STOCKFISH_PATH = "/opt/homebrew/bin/stockfish"  # macOS (Homebrew). Windows example: r"C:\Program Files\Stockfish\stockfish.exe"


# “Depth” = how many plies (half-moves) Stockfish looks ahead.
# Higher depth → stronger, but slower.
# Depth 10 means Stockfish looks ~10 half-moves into the future (≈ 5 full turns).
# Used after your own move to judge your move's quality 
ENGINE_DEPTH_ANALYZE = 8      


#Used to gauge and decide the coach's move
ENGINE_DEPTH_REPLY   = 8       # depth for the coach's reply

# cp = centipawn, where 100 = value of 1 pawn

# Move category thresholds based on centipawn loss (delta_cp = after - before):
# < 50 cp loss → Good
# 50–100 cp loss → Inaccuracy
# 100–300 cp loss → Mistake
# 300+ cp loss → Blunder
#
# Our labeling helper compares against negative deltas (loss is negative), so we
# encode the boundaries as negatives accordingly:
BLUNDER_THRESHOLDS   = (-50, -100, -300)  # good, inaccuracy, mistake, blunder
