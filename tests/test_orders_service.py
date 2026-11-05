from pydantic import ValidationError
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location("quickbite_orders_testmod", Path(__file__).parents[1]/"main.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OrderRequest=module.OrderRequest

def test_valid_order():
    o=OrderRequest(customerId='  alice  ',restaurantId='r1',items=[{'menuItemId':'m1','quantity':2}])
    assert o.customerId=='alice'

def test_invalid_quantity():
    try:OrderRequest(customerId='alice',restaurantId='r1',items=[{'menuItemId':'m1','quantity':0}])
    except ValidationError:return
    raise AssertionError('Expected quantity validation')
