{
    'name': 'Shopify Connector',
    'version': '19.0.1.0.0',
    'category': 'eCommerce',
    'summary': 'Connect Shopify with Odoo — products, orders, inventory, customers',
    'description': """
Shopify Connector for Odoo 19
==============================

Connect your Shopify store with Odoo for seamless synchronization of:

* **Products** — sync products and variants from Odoo to Shopify
* **Inventory** — real-time inventory synchronization
* **Orders** — automatic order import with customer creation
* **Fulfillment** — send tracking codes back to Shopify
* **Returns** — process returns from Shopify or Odoo
* **Accounting** — automatic invoicing and credit notes

Key Features
------------
* GraphQL API 2026-04 support
* Webhook-based real-time updates
* Multi-location inventory support
* Carrier mapping for shipping methods
* GDPR compliant
    """,
    'author': 'Source IT',
    'website': 'https://source-it.nu',
    'license': 'OPL-1',
    'price': 149.0,
    'currency': 'EUR',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'web',
        'delivery',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/shopify_config_views.xml',
        'views/shopify_product_views.xml',
        'data/shopify_cron.xml',
    ],
    'images': [
        'static/description/banner.png',
    ],
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': True,
    'auto_install': False,
}
