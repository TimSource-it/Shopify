from odoo import models, fields


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_order_id = fields.Char(
        string='Shopify Order ID',
        copy=False,
        index=True,
    )
    shopify_order_number = fields.Char(
        string='Shopify Bestelnummer',
        copy=False,
    )
    shopify_financial_status = fields.Char(
        string='Shopify Betaalstatus',
        copy=False,
    )
    shopify_fulfillment_status = fields.Char(
        string='Shopify Verzendstatus',
        copy=False,
    )
