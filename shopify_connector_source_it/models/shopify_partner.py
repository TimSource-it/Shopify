from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_customer_id = fields.Char(
        string='Shopify Customer ID',
        copy=False,
        index=True,
    )

    _sql_constraints = [
        ('shopify_customer_id_unique',
         'UNIQUE(shopify_customer_id)',
         'A customer with this Shopify ID already exists.')
    ]
