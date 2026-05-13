from odoo import http
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)


class ShopifyOAuthController(http.Controller):

    @http.route('/shopify/test', type='http', auth='none')
    def shopify_test(self, **kwargs):
        return 'Shopify controller werkt!'

    @http.route('/shopify/install', type='http', auth='none', csrf=False)
    def shopify_install(self, **kwargs):
        """Ontvangt het verzoek van Shopify na installatie en start OAuth flow."""
        shop = kwargs.get('shop')
        if not shop:
            return 'Geen winkel opgegeven.'

        shop_name = shop.replace('.myshopify.com', '')
        env = request.env(user=1)

        config = env['shopify.config'].search([
            ('shop_name', '=', shop_name)
        ], limit=1)

        if not config:
            _logger.warning(f"Geen configuratie gevonden voor winkel: {shop_name}")
            return request.redirect('/web')

        oauth_url = config._build_oauth_url(shop)
        return request.redirect(oauth_url)

    @http.route('/shopify/callback', type='http', auth='none', csrf=False)
    def shopify_callback(self, **kwargs):
        """Verwerkt de OAuth callback van Shopify."""
        code = kwargs.get('code')
        shop = kwargs.get('shop')
        state = kwargs.get('state')

        _logger.info(f"Shopify callback: shop={shop}, code={code[:10] if code else None}")

        if not code or not shop:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&error=missing_params')

        shop_name = shop.replace('.myshopify.com', '')
        env = request.env(user=1)

        config = env['shopify.config'].search([
            ('shop_name', '=', shop_name)
        ], limit=1)

        if not config and state:
            try:
                config = env['shopify.config'].browse(int(state))
            except Exception:
                pass

        if not config:
            config = env['shopify.config'].create({
                'shop_name': shop_name,
            })

        success = config._exchange_code_for_token(code, shop)

        if success:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&success=1')
        else:
            return request.redirect('/web#action=shopify_connector.action_shopify_config&error=token_exchange_failed')
