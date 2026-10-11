"""Run the example customer's order review before Reins integration."""

import json

from business import review_orders

if __name__ == "__main__":
    print(json.dumps(review_orders(), indent=2))
