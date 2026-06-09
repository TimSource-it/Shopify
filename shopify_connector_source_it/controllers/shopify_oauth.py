from odoo import http
from odoo.http import request
import hmac
import hashlib
import logging
import re
import secrets

_logger = logging.getLogger(__name__)


def verify_shopify_hmac(query_string, secret):
    """
    Verify Shopify HMAC via raw query string.
    Shopify-compliant: strip only hmac parameter, use rest as-is.
    """
    try:
        hmac_received = None
        parts = []
        for part in query_string.split('&'):
            if part.startswith('hmac='):
                hmac_received = part.split('=', 1)[1]
            else:
                parts.append(part)

        if not hmac_received:
            return False

        message = '&'.join(sorted(parts))
        digest = hmac.new(
            secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(digest, hmac_received)
    except Exception as e:
        _logger.error(f"HMAC verification error: {e}")
        return False


def sanitize_shop(shop):
    """Validate and normalize the shop parameter."""
    if not shop:
        return None
    shop = shop.strip().lower()
    shop = re.sub(r'^https?://', '', shop).rstrip('/')
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$', shop):
        return None
    return shop


class ShopifyOAuthController(http.Controller):

    def _get_client_secret(self, shop_name=None):
        """Get client secret — per shop or global."""
        env = request.env['ir.config_parameter'].sudo()
        if shop_name:
            secret = env.get_param(f'shopify.client_secret.{shop_name}')
            if secret:
                return secret
        return env.get_param('shopify.client_secret')

    @http.route('/shopify/test', type='http', auth='none')
    def shopify_test(self, **kwargs):
        return 'Shopify connector is working!'

    @http.route('/shopify/success', type='http', auth='public', csrf=False)
    def shopify_success(self, **kwargs):
        shop = kwargs.get('shop', '')
        return f"""<!DOCTYPE html>
        <html><head><title>Shopify Connection</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px">
        <h1>✅ Shopify connection successful!</h1>
        <p>Shop <b>{shop}</b> has been successfully connected to Odoo.</p>
        <p>You can close this window and return to Odoo.</p>
        </body></html>"""

    @http.route('/shopify/error', type='http', auth='public', csrf=False)
    def shopify_error(self, **kwargs):
        error = kwargs.get('error', 'unknown')
        return f"""<!DOCTYPE html>
        <html><head><title>Shopify Error</title></head>
        <body style="font-family:sans-serif;text-align:center;padding:50px">
        <h1>❌ Shopify connection failed</h1>
        <p>Error: <b>{error}</b></p>
        <p>Please contact your administrator.</p>
        </body></html>"""

    @http.route('/shopify/install', type='http', auth='public', csrf=False)
    def shopify_install(self, **kwargs):
        shop = sanitize_shop(kwargs.get('shop'))
        if not shop:
            return request.redirect('/shopify/error?error=invalid_shop')

        shop_name = shop.replace('.myshopify.com', '')

        client_secret = self._get_client_secret(shop_name)
        if client_secret:
            query_string = request.httprequest.query_string.decode('utf-8')
            if not verify_shopify_hmac(query_string, client_secret):
                _logger.warning(f"HMAC verification failed for: {shop_name}")
                return request.redirect('/shopify/error?error=invalid_hmac')

        config = request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name)
        ], limit=1)

        if not config:
            _logger.warning(f"No configuration found for shop: {shop_name}")
            return request.redirect('/shopify/error?error=shop_not_found')

        state = request.env['shopify.oauth.state'].sudo().create_state(shop_name)
        oauth_url = config.sudo()._build_oauth_url(shop, state)
        return request.redirect(oauth_url)

    @http.route('/shopify/callback', type='http', auth='public', csrf=False)
    def shopify_callback(self, **kwargs):
        shop = sanitize_shop(kwargs.get('shop'))
        code = kwargs.get('code')
        state = kwargs.get('state')

        if not code or not shop or not state:
            return request.redirect('/shopify/error?error=missing_params')

        shop_name = shop.replace('.myshopify.com', '')

        state_shop = request.env['shopify.oauth.state'].sudo().validate_and_consume(state)
        if not state_shop or state_shop != shop_name:
            _logger.warning(f"State validation failed for: {shop_name}")
            return request.redirect('/shopify/error?error=invalid_state')

        client_secret = self._get_client_secret(shop_name)
        if client_secret:
            query_string = request.httprequest.query_string.decode('utf-8')
            if 'hmac=' in query_string:
                if not verify_shopify_hmac(query_string, client_secret):
                    _logger.warning(f"Callback HMAC failed for: {shop_name}")
                    return request.redirect('/shopify/error?error=invalid_hmac')

        config = request.env['shopify.config'].sudo().search([
            ('shop_name', '=', shop_name)
        ], limit=1)

        if not config:
            return request.redirect('/shopify/error?error=shop_not_found')

        success = config.sudo()._exchange_code_for_token(code, shop)

        if success:
            return request.redirect(f'/shopify/success?shop={shop_name}')
        else:
            return request.redirect('/shopify/error?error=token_exchange_failed')
