from odoo import models, fields, api
import secrets


class ShopifyOAuthState(models.TransientModel):
    _name = 'shopify.oauth.state'
    _description = 'Temporary OAuth State for Shopify'
    _transient_max_hours = 0.17  # 10 minutes

    state = fields.Char(string='State Token', required=True, index=True)
    shop_name = fields.Char(string='Shop Name', required=True)

    @api.model
    def create_state(self, shop_name):
        """Create a new state and return the token."""
        state = secrets.token_hex(32)
        self.create({'state': state, 'shop_name': shop_name})
        return state

    @api.model
    def validate_and_consume(self, state):
        """Validate state, delete it and return shop_name."""
        record = self.search([('state', '=', state)], limit=1)
        if not record:
            return None
        shop_name = record.shop_name
        record.unlink()
        return shop_name
