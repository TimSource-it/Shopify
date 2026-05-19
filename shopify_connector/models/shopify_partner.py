from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_customer_id = fields.Char(
        string='Shopify Klant ID',
        copy=False,
        index=True,
    )
