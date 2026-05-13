from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)


class ShopifyOAuthController(http.Controller):

    @http.route('/shopify/test', type='http', auth='none')
    def shopify_test(self, **kwargs):
        return 'Shopify controller werkt!'

    @http.route('/shopify/callback', type='http', auth='public', website=False)
    def shopify_callback(self, **kwargs):
        """Verwerkt de OAuth callback van Shopify."""
        code = kwargs.get('code')
        shop = kwargs.get('shop')
        state = kwargs.get('state')

        if not code or not shop:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&error=missing_params')

        shop_name = shop.replace('.myshopify.com', '')
        config = request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name)
        ], limit=1)

        if not config:
            config = request.env['shopify.config'].sudo().create({
                'shop_name': shop_name,
            })

        success = config.sudo()._exchange_code_for_token(code, shop)

        if success:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&success=1')
        else:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&error=token_exchange_failed')
