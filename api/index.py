import sys
import os

# Add parent directory to path so dashboard and db modules can be imported seamlessly
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import app
