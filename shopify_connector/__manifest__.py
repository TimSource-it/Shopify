{
    'name': 'Shopify Connector',
    'version': '19.0.1.0.0',
    'category': 'eCommerce',
    'summary': 'Koppel Shopify met Odoo — producten, bestellingen, voorraad, klanten',
    'author': 'Source IT',
    'website': 'https://source-it.nu',
    'license': 'OPL-1',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/shopify_config_views.xml',
        'data/shopify_cron.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
