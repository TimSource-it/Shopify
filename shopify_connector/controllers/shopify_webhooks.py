@http.route('/shopify/webhooks/app/uninstalled',
                type='http', auth='public', csrf=False, methods=['POST'])
    def app_uninstalled(self, **kwargs):
        """Cleanup bij verwijdering van de app."""
        try:
            data = request.httprequest.data
            hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

            if not self._verify_webhook(data, hmac_header):
                _logger.warning("Webhook HMAC verificatie mislukt voor app/uninstalled")
                return request.make_response('Unauthorized', status=401)

            payload = json.loads(data)
            shop_domain = payload.get('domain', '')
            shop_name = shop_domain.replace('.myshopify.com', '')

            config = request.env['shopify.config'].sudo().search([
                ('shop_name', '=', shop_name)
            ], limit=1)

            if config:
                config.sudo()._unregister_carrier_service()
                config.sudo().write({
                    'access_token': False,
                    'refresh_token': False,
                    'access_token_expires_at': False,
                    'state': 'draft',
                    'shopify_carrier_service_id': False,
                })
                _logger.info(f"App verwijderd voor winkel: {shop_name}")

            return request.make_response('OK', status=200)
        except Exception as e:
            _logger.error(f"app/uninstalled fout: {e}")
            return request.make_response('OK', status=200)
