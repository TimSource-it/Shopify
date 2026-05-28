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
        domain = [('state', '=', 'connected')]
        if shop_name:
            domain.append(('shop_name', '=', shop_name))
        return self.env['shopify.config'].search(domain, limit=1)

    @api.model
    def _get_price(self, variant, config):
        if config.pricelist_id:
            try:
                return config.pricelist_id._get_product_price(variant, 1.0)
            except Exception as e:
                _logger.warning(f"Prijslijst prijs ophalen mislukt: {e}")
                return variant.lst_price
        return variant.lst_price

    @api.model
    def _get_product_images(self, product):
        """Haal productafbeeldingen op als base64."""
        images = []
        if product.image_1920:
            try:
                img_data = product.image_1920
                if isinstance(img_data, bytes):
                    img_data = img_data.decode('utf-8')
                images.append(img_data)
            except Exception as e:
                _logger.warning(f"Hoofdafbeelding ophalen mislukt: {e}")
        try:
            for extra_img in product.product_template_image_ids:
                if extra_img.image_1920:
                    try:
                        img_data = extra_img.image_1920
                        if isinstance(img_data, bytes):
                            img_data = img_data.decode('utf-8')
                        images.append(img_data)
                    except Exception as e:
                        _logger.warning(f"Extra afbeelding ophalen mislukt: {e}")
        except Exception as e:
            _logger.warning(f"Extra afbeeldingen ophalen mislukt: {e}")
        return images

    @api.model
    def _get_description(self, product):
        if product.shopify_description:
            return product.shopify_description
        if hasattr(product, 'website_description') and product.website_description:
            return product.website_description
        if product.description_sale:
            return product.description_sale
        return ''

    @api.model
    def _get_variant_options(self, product):
        """Haal attribuutnamen op voor Shopify options (max 3)."""
        options = []
        for line in product.attribute_line_ids:
            options.append(line.attribute_id.name)
        return options[:3]

    @api.model
    def _get_variant_option_values(self, variant):
        """Haal attribuutwaarden op voor een variant als option1/2/3."""
        values = {}
        for i, attr_value in enumerate(variant.product_template_attribute_value_ids):
            if i < 3:
                values[f'option{i + 1}'] = attr_value.name
        return values

    @api.model
    def _get_variant_option_key(self, variant):
        """Maak een unieke sleutel van de attribuutwaarden voor matching."""
        values = [
            attr_value.name
            for attr_value in variant.product_template_attribute_value_ids
        ]
        return '|'.join(values)

    @api.model
    def _match_shopify_variants(self, product, shopify_variants):
        """Koppel Shopify varianten terug aan Odoo varianten via SKU of attribuutwaarden."""
        shopify_by_sku = {}
        shopify_by_options = {}

        for sv in shopify_variants:
            sku = sv.get('sku', '')
            if sku:
                shopify_by_sku[sku] = sv
            option_parts = []
            for key in ['option1', 'option2', 'option3']:
                val = sv.get(key)
                if val and val != 'Default Title':
                    option_parts.append(val)
            if option_parts:
                option_key = '|'.join(option_parts)
                shopify_by_options[option_key] = sv

        for variant in product.product_variant_ids:
            matched = None

            if variant.shopify_variant_id:
                for sv in shopify_variants:
                    if str(sv.get('id')) == str(variant.shopify_variant_id):
                        matched = sv
                        break

            if not matched and variant.default_code:
                matched = shopify_by_sku.get(variant.default_code)

            if not matched:
                option_key = self._get_variant_option_key(variant)
                if option_key:
                    matched = shopify_by_options.get(option_key)

            if matched:
                variant.write({
                    'shopify_variant_id': str(matched.get('id')),
                    'shopify_inventory_item_id': str(matched.get('inventoryItem', {}).get('id', '').split('/')[-1] if isinstance(matched.get('inventoryItem'), dict) else matched.get('inventory_item_id', '')),
                })
                _logger.info(f"Variant {variant.name} gekoppeld aan Shopify variant {matched.get('id')}")
            else:
                _logger.warning(f"Geen Shopify variant gevonden voor {variant.name}")

    @api.model
    def _get_shopify_sellable_qty(self, variant, warehouse):
        """Haal verkoopbare voorraad op — fysiek minus gereserveerd."""
        location = warehouse.lot_stock_id
        quants = self.env['stock.quant'].search([
            ('product_id', '=', variant.id),
            ('location_id', '=', location.id),
        ])
        qty = sum(quants.mapped('available_quantity'))
        return max(0, int(qty))

    @api.model
    def sync_inventory_to_shopify(self, product_tmpl_id, config=None):
        """Synchroniseer voorraad van Odoo naar Shopify via GraphQL."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie gevonden")
            return False

        if not config.sync_inventory:
            return True

        product = self.env['product.template'].browse(product_tmpl_id)
        if not product.exists():
            return False

        location_mappings = self.env['shopify.location'].search([
            ('config_id', '=', config.id),
            ('sync_inventory', '=', True),
            ('warehouse_id', '!=', False),
        ])

        if not location_mappings:
            self.env.cr.execute(
                "SELECT shopify_location_id FROM shopify_config WHERE id = %s",
                (config.id,)
            )
            row = self.env.cr.fetchone()
            fallback_location = row[0] if row else False

            if not fallback_location:
                _logger.error("Geen Shopify locatie gevonden")
                return False

            for variant in product.product_variant_ids:
                if not variant.shopify_inventory_item_id:
                    continue
                try:
                    qty = max(0, int(variant.qty_available))
                    self._set_inventory_graphql(
                        config,
                        variant.shopify_inventory_item_id,
                        fallback_location,
                        qty
                    )
                except Exception as e:
                    _logger.error(f"Voorraad sync fout: {e}")
            return True

        success = True
        for mapping in location_mappings:
            for variant in product.product_variant_ids:
                if not variant.shopify_inventory_item_id:
                    continue
                try:
                    qty = self._get_shopify_sellable_qty(variant, mapping.warehouse_id)
                    result = self._set_inventory_graphql(
                        config,
                        variant.shopify_inventory_item_id,
                        mapping.shopify_location_id,
                        qty
                    )
                    if result:
                        _logger.info(f"Voorraad {qty} gesynchroniseerd voor {variant.name} naar {mapping.shopify_location_name}")
                    else:
                        success = False
                except Exception as e:
                    _logger.error(f"Voorraad sync fout: {e}")
                    success = False

        return success

    @api.model
    def _set_inventory_graphql(self, config, inventory_item_id, location_id, qty):
        """Stel voorraad in via GraphQL mutation."""
        mutation = """
        mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
          inventorySetOnHandQuantities(input: $input) {
            userErrors {
              field
              message
            }
            inventoryAdjustmentGroup {
              reason
              changes {
                name
                delta
              }
            }
          }
        }
        """
        variables = {
            'input': {
                'reason': 'correction',
                'setQuantities': [{
                    'inventoryItemId': f"gid://shopify/InventoryItem/{inventory_item_id}",
                    'locationId': f"gid://shopify/Location/{location_id}",
                    'quantity': qty,
                }]
            }
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('inventorySetOnHandQuantities', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Voorraad GraphQL fout: {errors}")
                return False
            return True
        return False

    @api.model
    def cron_sync_pending_products(self):
        """Cron job: synchroniseer alle pending producten."""
        config = self._get_config()
        if not config:
            _logger.info("Geen actieve Shopify configuratie gevonden voor cron sync")
            return

        pending = self.env['product.template'].search([
            ('shopify_published', '=', True),
            ('shopify_sync_status', '=', 'pending'),
        ])

        _logger.info(f"Cron sync: {len(pending)} producten te synchroniseren")

        for product in pending:
            try:
                self.sync_product_to_shopify(product.id, config)
            except Exception as e:
                _logger.error(f"Cron sync fout voor {product.name}: {e}")

    @api.model
    def sync_product_to_shopify(self, product_tmpl_id, config=None):
        """Synchroniseer een product van Odoo naar Shopify via GraphQL."""
        if not config:
            config = self._get_config()
        if not config:
            _logger.error("Geen actieve Shopify configuratie gevonden")
            return False

        product = self.env['product.template'].browse(product_tmpl_id)
        if not product.exists():
            return False

        self.env.cr.execute(
            "SELECT shopify_product_id, shopify_published FROM product_template WHERE id = %s",
            (product.id,)
        )
        row = self.env.cr.fetchone()
        shopify_product_id = row[0] if row else False
        shopify_published = row[1] if row else False

        try:
            # Product op draft zetten als niet gepubliceerd
            if not shopify_published:
                if shopify_product_id:
                    return self._set_product_status(config, shopify_product_id, 'DRAFT', product)
                else:
                    _logger.info(f"Product {product.name} wordt niet gesynchroniseerd (niet gepubliceerd)")
                    return False

            # Bouw vendor op
            vendor = ''
            if product.seller_ids:
                vendor = product.seller_ids[0].partner_id.name
            else:
                vendor = self.env.company.name

            # Tags
            tags = []
            if product.shopify_tags:
                tags = [t.strip() for t in product.shopify_tags.split(',')]
            elif hasattr(product, 'tag_ids') and product.tag_ids:
                try:
                    tags = product.tag_ids.mapped('name')
                except Exception:
                    tags = []

            body_html = self._get_description(product)
            heeft_attributen = bool(product.attribute_line_ids)
            options = self._get_variant_options(product) if heeft_attributen else []

            if shopify_product_id:
                return self._update_product_graphql(
                    config, product, shopify_product_id,
                    vendor, tags, body_html, options, heeft_attributen
                )
            else:
                return self._create_product_graphql(
                    config, product,
                    vendor, tags, body_html, options, heeft_attributen
                )

        except Exception as e:
            _logger.error(f"Product sync fout: {e}")
            product.write({
                'shopify_sync_status': 'error',
                'shopify_sync_error': str(e),
            })
            return False

    @api.model
    def _set_product_status(self, config, shopify_product_id, status, product):
        """Zet product status via GraphQL."""
        mutation = """
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product {
              id
              status
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        variables = {
            'input': {
                'id': f"gid://shopify/Product/{shopify_product_id}",
                'status': status,
            }
        }
        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('productUpdate', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Product status fout: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False
            product.write({
                'shopify_sync_status': 'synced',
                'shopify_last_sync': fields.Datetime.now(),
            })
            _logger.info(f"Product {product.name} status gezet op {status}")
            return True
        return False

    @api.model
    def _build_variant_input(self, variant, config, heeft_attributen, position=None):
        """Bouw variant input op voor GraphQL."""
        price = self._get_price(variant, config)
        variant_input = {
            'price': str(price),
            'sku': variant.default_code or '',
            'inventoryPolicy': 'CONTINUE' if config.allow_backorder else 'DENY',
            'inventoryManagement': 'SHOPIFY',
        }

        if heeft_attributen:
            option_values = self._get_variant_option_values(variant)
            if 'option1' in option_values:
                variant_input['option1'] = option_values['option1']
            if 'option2' in option_values:
                variant_input['option2'] = option_values['option2']
            if 'option3' in option_values:
                variant_input['option3'] = option_values['option3']

        if hasattr(variant, 'barcode') and variant.barcode:
            variant_input['barcode'] = variant.barcode

        if variant.shopify_variant_id:
            variant_input['id'] = f"gid://shopify/ProductVariant/{variant.shopify_variant_id}"

        if position is not None:
            variant_input['position'] = position + 1

        return variant_input

    @api.model
    def _create_product_graphql(self, config, product, vendor, tags, body_html, options, heeft_attributen):
        """Maak nieuw product aan via GraphQL."""
        mutation = """
        mutation productCreate($input: ProductInput!, $media: [CreateMediaInput!]) {
          productCreate(input: $input, media: $media) {
            product {
              id
              legacyResourceId
              variants(first: 100) {
                edges {
                  node {
                    id
                    legacyResourceId
                    sku
                    selectedOptions {
                      name
                      value
                    }
                    inventoryItem {
                      id
                      legacyResourceId
                    }
                  }
                }
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """

        product_input = {
            'title': product.name,
            'descriptionHtml': body_html,
            'vendor': vendor,
            'productType': product.categ_id.name or '',
            'status': 'ACTIVE',
            'tags': tags,
            'variants': [],
        }

        if options:
            product_input['options'] = options

        for i, variant in enumerate(product.product_variant_ids):
            variant_input = self._build_variant_input(variant, config, heeft_attributen, i)
            product_input['variants'].append(variant_input)

        # Afbeeldingen
        media_input = []
        images = self._get_product_images(product)
        for img_data in images:
            media_input.append({
                'mediaContentType': 'IMAGE',
                'originalSource': f"data:image/jpeg;base64,{img_data}",
            })

        variables = {'input': product_input}
        if media_input:
            variables['media'] = media_input

        data = config._graphql(mutation, variables)
        if data:
            errors = data.get('productCreate', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Product aanmaken fout: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False

            shopify_product = data.get('productCreate', {}).get('product', {})
            shopify_product_id = shopify_product.get('legacyResourceId')

            # Koppel varianten terug
            shopify_variants = [
                edge['node']
                for edge in shopify_product.get('variants', {}).get('edges', [])
            ]
            self._match_shopify_variants_graphql(product, shopify_variants)

            product.write({
                'shopify_product_id': str(shopify_product_id),
                'shopify_last_sync': fields.Datetime.now(),
                'shopify_sync_status': 'synced',
                'shopify_sync_error': False,
            })
            _logger.info(f"Product {product.name} aangemaakt in Shopify: {shopify_product_id}")

            if config.sync_inventory:
                self.sync_inventory_to_shopify(product.id, config)

            return True
        return False

    @api.model
    def _update_product_graphql(self, config, product, shopify_product_id, vendor, tags, body_html, options, heeft_attributen):
        """Bestaand product bijwerken via GraphQL."""
        mutation = """
        mutation productUpdate($input: ProductInput!) {
          productUpdate(input: $input) {
            product {
              id
              legacyResourceId
              variants(first: 100) {
                edges {
                  node {
                    id
                    legacyResourceId
                    sku
                    selectedOptions {
                      name
                      value
                    }
                    inventoryItem {
                      id
                      legacyResourceId
                    }
                  }
                }
              }
            }
            userErrors {
              field
              message
            }
          }
        }
        """

        product_input = {
            'id': f"gid://shopify/Product/{shopify_product_id}",
            'title': product.name,
            'descriptionHtml': body_html,
            'vendor': vendor,
            'productType': product.categ_id.name or '',
            'status': 'ACTIVE',
            'tags': tags,
            'variants': [],
        }

        if options:
            product_input['options'] = options

        for i, variant in enumerate(product.product_variant_ids):
            variant_input = self._build_variant_input(variant, config, heeft_attributen, i)
            product_input['variants'].append(variant_input)

        data = config._graphql(mutation, {'input': product_input})
        if data:
            errors = data.get('productUpdate', {}).get('userErrors', [])
            if errors:
                _logger.error(f"Product bijwerken fout: {errors}")
                product.write({'shopify_sync_status': 'error', 'shopify_sync_error': str(errors)})
                return False

            shopify_product = data.get('productUpdate', {}).get('product', {})
            shopify_variants = [
                edge['node']
                for edge in shopify_product.get('variants', {}).get('edges', [])
            ]
            self._match_shopify_variants_graphql(product, shopify_variants)

            product.write({
                'shopify_last_sync': fields.Datetime.now(),
                'shopify_sync_status': 'synced',
                'shopify_sync_error': False,
            })
            _logger.info(f"Product {product.name} bijgewerkt in Shopify")

            if config.sync_inventory:
                self.sync_inventory_to_shopify(product.id, config)

            return True
        return False

    @api.model
    def _match_shopify_variants_graphql(self, product, shopify_variants):
        """Koppel GraphQL Shopify varianten terug aan Odoo varianten."""
        shopify_by_sku = {}
        shopify_by_options = {}

        for sv in shopify_variants:
            sku = sv.get('sku', '')
            if sku:
                shopify_by_sku[sku] = sv
            option_parts = [
                opt['value']
                for opt in sv.get('selectedOptions', [])
                if opt['value'] != 'Default Title'
            ]
            if option_parts:
                option_key = '|'.join(option_parts)
                shopify_by_options[option_key] = sv

        for variant in product.product_variant_ids:
            matched = None

            # 1. Match op bestaand shopify_variant_id
            if variant.shopify_variant_id:
                for sv in shopify_variants:
                    legacy_id = sv.get('legacyResourceId')
                    if str(legacy_id) == str(variant.shopify_variant_id):
                        matched = sv
                        break

            # 2. Match op SKU
            if not matched and variant.default_code:
                matched = shopify_by_sku.get(variant.default_code)

            # 3. Match op attribuutwaarden
            if not matched:
                option_key = self._get_variant_option_key(variant)
                if option_key:
                    matched = shopify_by_options.get(option_key)

            if matched:
                inventory_item_legacy_id = matched.get('inventoryItem', {}).get('legacyResourceId', '')
                variant.write({
                    'shopify_variant_id': str(matched.get('legacyResourceId')),
                    'shopify_inventory_item_id': str(inventory_item_legacy_id),
                })
                _logger.info(f"Variant {variant.name} gekoppeld: variant={matched.get('legacyResourceId')}, inventory={inventory_item_legacy_id}")
            else:
                _logger.warning(f"Geen Shopify variant gevonden voor {variant.name}")
