from odoo import models, fields, api
import requests
import logging
import base64

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
    def _get_price(self, variant, config):
        """Haal de prijs op via prijslijst of standaard verkoopprijs."""
        if config.pricelist_id:
            try:
                return config.pricelist_id._get_product_price(variant, 1.0)
            except Exception as e:
                _logger.warning(f"Prijslijst prijs ophalen mislukt, gebruik standaard prijs: {e}")
                return variant.lst_price
        return variant.lst_price

    @api.model
    def _get_product_images(self, product):
        """Haal productafbeeldingen op als base64."""
        images = []

        # Hoofdafbeelding
        if product.image_1920:
            try:
                img_data = product.image_1920
                if isinstance(img_data, bytes):
                    img_data = img_data.decode('utf-8')
                images.append({'attachment': img_data})
            except Exception as e:
                _logger.warning(f"Hoofdafbeelding ophalen mislukt: {e}")

        # Extra afbeeldingen
        try:
            for extra_img in product.product_template_image_ids:
                if extra_img.image_1920:
                    try:
                        img_data = extra_img.image_1920
                        if isinstance(img_data, bytes):
                            img_data = img_data.decode('utf-8')
                        images.append({'attachment': img_data})
                    except Exception as e:
                        _logger.warning(f"Extra afbeelding ophalen mislukt: {e}")
        except Exception as e:
            _logger.warning(f"Extra afbeeldingen ophalen mislukt: {e}")

        return images

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

        # Haal shopify velden direct uit DB om cache problemen te voorkomen
        self.env.cr.execute(
            "SELECT shopify_product_id, shopify_published FROM product_template WHERE id = %s",
            (product.id,)
        )
        row = self.env.cr.fetchone()
        shopify_product_id = row[0] if row else False
        shopify_published = row[1] if row else False

        try:
            # Check of product gepubliceerd mag worden
            if not shopify_published:
                if shopify_product_id:
                    # Zet op draft in Shopify
                    url = f"{config.shop_url}/admin/api/2025-01/products/{shopify_product_id}.json"
                    response = requests.put(
                        url,
                        json={'product': {'id': shopify_product_id, 'status': 'draft'}},
                        headers=config._get_headers(),
                        timeout=15
                    )
                    if response.status_code == 200:
                        product.write({
                            'shopify_sync_status': 'synced',
                            'shopify_last_sync': fields.Datetime.now(),
                        })
                        _logger.info(f"Product {product.name} op draft gezet in Shopify")
                        return True
                    else:
                        error = response.text[:200]
                        product.write({
                            'shopify_sync_status': 'error',
                            'shopify_sync_error': error,
                        })
                        _logger.error(f"Product draft mislukt: {error}")
                        return False
                else:
                    _logger.info(f"Product {product.name} wordt niet gesynchroniseerd (niet gepubliceerd)")
                    return False

            # Bepaal vendor
            vendor = ''
            if product.seller_ids:
                vendor = product.seller_ids[0].partner_id.name
            else:
                vendor = self.env.company.name

            # Bepaal tags
            tags = ''
            if product.shopify_tags:
                tags = product.shopify_tags
            elif hasattr(product, 'tag_ids') and product.tag_ids:
                try:
                    tags = ','.join(product.tag_ids.mapped('name'))
                except Exception:
                    tags = ''

            # Bouw product data op
            product_data = {
                'product': {
                    'title': product.name,
                    'body_html': product.description_sale or '',
                    'vendor': vendor,
                    'product_type': product.categ_id.name or '',
                    'status': 'active',
                    'tags': tags,
                    'variants': [],
                }
            }

            # Voeg afbeeldingen toe
            images = self._get_product_images(product)
            if images:
                product_data['product']['images'] = images

            # Voeg varianten toe
            for variant in product.product_variant_ids:
                price = self._get_price(variant, config)

                variant_data = {
                    'price': str(price),
                    'sku': variant.default_code or '',
                    'inventory_management': 'shopify',
                    'inventory_policy': 'continue' if config.allow_backorder else 'deny',
                }

                # Barcode indien beschikbaar
                if hasattr(variant, 'barcode') and variant.barcode:
                    variant_data['barcode'] = variant.barcode

                # Gewicht indien beschikbaar
                if hasattr(product, 'weight') and product.weight:
                    variant_data['weight'] = product.weight
                    variant_data['weight_unit'] = 'kg'

                if variant.shopify_variant_id:
                    variant_data['id'] = variant.shopify_variant_id

                product_data['product']['variants'].append(variant_data)

            # Nieuw product of update?
            if shopify_product_id:
                url = f"{config.shop_url}/admin/api/2025-01/products/{shopify_product_id}.json"
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
