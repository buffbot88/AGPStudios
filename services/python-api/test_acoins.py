import sys
from pathlib import Path
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy


def demo():
    assert proxy.ACOINS_PACKAGES == {1000: (100, 1000), 5000: (500, 5000), 10000: (1000, 10000)}
    order = {'id': 'local-order', 'paypal_order_id': 'PAYPAL-ORDER-1', 'amount_cents': 500}
    result = {'id': 'PAYPAL-ORDER-1', 'status': 'COMPLETED', 'purchase_units': [{
        'reference_id': 'local-order', 'payments': {'captures': [{
            'id': 'capture-1', 'status': 'COMPLETED',
            'amount': {'currency_code': 'USD', 'value': '5.00'},
        }]},
    }]}
    assert proxy.completed_paypal_capture(order, result) == 'capture-1'
    result['purchase_units'][0]['payments']['captures'][0]['amount']['value'] = '4.99'
    assert proxy.completed_paypal_capture(order, result) is None
    previous = proxy.CONFIG["payment_encryption_key"]
    proxy.CONFIG["payment_encryption_key"] = Fernet.generate_key().decode()
    encrypted = proxy.payment_cipher().encrypt(b'paypal-secret')
    assert proxy.payment_cipher().decrypt(encrypted) == b'paypal-secret'
    proxy.CONFIG["payment_encryption_key"] = previous
    print('ACoins package, capture, and secret encryption: PASS')


if __name__ == '__main__':
    demo()
