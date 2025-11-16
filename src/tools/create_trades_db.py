import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from trade_manager import TradeManager
if __name__=='__main__':
    TradeManager()
    print('\\nDB ready')
