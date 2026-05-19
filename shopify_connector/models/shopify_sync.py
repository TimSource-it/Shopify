from odoo import models, fields, api
import requests
import logging

_logger = logging.getLogger(__name__)


class ShopifySync(models.AbstractModel):
    _name = 'shopify.sync'
    _description = 'Shopify Synchronisatie'

    @api.model
    def _get_config(self, shop_name=None):
        """Haal de actieve Shopify configuratie op."""
        domain = [('state', '=', 'connected')]
        if shop_name:
            domain.append(('shop_name', '=', shop_name))
        return self.env['shopify.config'].search(domain, limit=1)

    @api.model
    def sync_product_to_shopify(self, product_tmpl_id, config=None):
        """Synchroniseer een product van Odoo naar Shopify."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie gevonden")
            return False

        product = self.env['product.template'].browse(product_tmpl_id)
        if not product.exists():
            return False

        try:
            # Bepaal vendor
            vendor = ''
            if product.seller_ids:
                vendor = product.seller_ids[0].partner_id.name
            else:
                vendor = self.env.company.name

            # Bouw product data op
            product_data = {
                'product': {
                    'title': product.name,
                    'body_html': product.description_sale or '',
                    'vendor': vendor,
                    'product_type': product.categ_id.name or '',
                    'status': 'active' if product.active else 'draft',
                    'variants': [],
                }
            }

            # Voeg varianten toe
            for variant in product.product_variant_ids:
                variant_data = {
                    'price': str(variant.lst_price),
                    'sku': variant.default_code or '',
                    'inventory_management': 'shopify',
                    'inventory_policy': 'continue' if config.allow_backorder else 'deny',
                }
                if variant.shopify_variant_id:
                    variant_data['id'] = variant.shopify_variant_id
                product_data['product']['variants'].append(variant_data)

            # Nieuw product of update?
            if product.shopify_product_id:
                url = f"{config.shop_url}/admin/api/2025-01/products/{product.shopify_product_id}.json"
                response = requests.put(
                    url,
                    json=product_data,
                    headers=config._get_headers(),
                    timeout=15
                )
            else:
                url = f"{config.shop_url}/admin/api/2025-01/products.json"
                response = requests.post(
                    url,
                    json=product_data,
                    headers=config._get_headers(),
                    timeout=15
                )

            if response.status_code in (200, 201):
                shopify_product = response.json().get('product', {})
                vals = {
                    'shopify_product_id': str(shopify_product.get('id')),
                    'shopify_last_sync': fields.Datetime.now(),
                    'shopify_sync_status': 'synced',
                    'shopify_sync_error': False,
                }
                # Sla variant IDs op
                shopify_variants = shopify_product.get('variants', [])
                for i, variant in enumerate(product.product_variant_ids):
                    if i < len(shopify_variants):
                        variant.write({
                            'shopify_variant_id': str(shopify_variants[i].get('id')),
                            'shopify_inventory_item_id': str(shopify_variants[i].get('inventory_item_id')),
                        })
                product.write(vals)
                _logger.info(f"Product {product.name} gesynchroniseerd naar Shopify")
                return True
            else:
                error = response.text[:200]
                product.write({
                    'shopify_sync_status': 'error',
                    'shopify_sync_error': error,
                })
                _logger.error(f"Product sync mislukt: {error}")
                return False

        except Exception as e:
            _logger.error(f"Product sync fout: {e}")
            product.write({
                'shopify_sync_status': 'error',
                'shopify_sync_error': str(e),
            })
            return False
