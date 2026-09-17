import sys
import os

# Root directory ko Python path me add karein taaki app.py import ho sake
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app