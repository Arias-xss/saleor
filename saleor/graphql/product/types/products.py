from dataclasses import asdict

import graphene
from django.conf import settings
from graphene import relay
from graphene.types.resolver import get_default_resolver
from graphene_django.types import DjangoObjectType
from graphene_federation import key
from graphql.error import GraphQLError

from ....core.permissions import ProductPermissions
from ....core.weight import convert_weight_to_default_weight_unit
from ....product import models
from ....product.templatetags.product_images import (
    get_product_image_thumbnail,
    get_thumbnail,
)
from ....product.utils import calculate_revenue_for_variant
from ....product.utils.availability import (
    get_product_availability,
    get_variant_availability,
)
from ....product.utils.costs import get_margin_for_variant, get_product_costs_data
from ....warehouse.availability import (
    get_available_quantity,
    get_quantity_allocated,
    is_product_in_stock,
)
from ...account.enums import CountryCodeEnum
from ...channel import ChannelContext, ChannelQsContext
from ...channel.utils import get_default_channel_or_graphql_error
from ...core.connection import CountableDjangoObjectType
from ...core.enums import ReportingPeriod, TaxRateType
from ...core.fields import (
    ChannelContextFilterConnectionField,
    FilterInputConnectionField,
    PrefetchingConnectionField,
)
from ...core.types import Image, Money, MoneyRange, TaxedMoney, TaxedMoneyRange, TaxType
from ...decorators import permission_required
from ...discount.dataloaders import DiscountsByDateTimeLoader
from ...meta.deprecated.resolvers import resolve_meta, resolve_private_meta
from ...meta.types import ObjectWithMetadata
from ...translations.fields import TranslationField
from ...translations.types import (
    CategoryTranslation,
    CollectionTranslation,
    ProductTranslation,
    ProductVariantTranslation,
)
from ...utils import get_database_id
from ...utils.filters import reporting_period_to_date
from ...warehouse.dataloaders import (
    AvailableQuantityByProductVariantIdAndCountryCodeLoader,
)
from ...warehouse.types import Stock
from ..dataloaders import (
    CategoryByIdLoader,
    CollectionsByProductIdLoader,
    ImagesByProductIdLoader,
    ProductByIdLoader,
    ProductChannelListingByProductIdAndChanneSlugLoader,
    ProductChannelListingByProductIdLoader,
    ProductVariantsByProductIdLoader,
    SelectedAttributesByProductIdLoader,
    SelectedAttributesByProductVariantIdLoader,
)
from ..filters import AttributeFilterInput
from ..resolvers import resolve_attributes
from .attributes import Attribute, SelectedAttribute
from .channels import ProductChannelListing
from .digital_contents import DigitalContent


class Margin(graphene.ObjectType):
    start = graphene.Int()
    stop = graphene.Int()


class BasePricingInfo(graphene.ObjectType):
    on_sale = graphene.Boolean(description="Whether it is in sale or not.")
    discount = graphene.Field(
        TaxedMoney, description="The discount amount if in sale (null otherwise)."
    )
    discount_local_currency = graphene.Field(
        TaxedMoney, description="The discount amount in the local currency."
    )


class VariantPricingInfo(BasePricingInfo):
    discount_local_currency = graphene.Field(
        TaxedMoney, description="The discount amount in the local currency."
    )
    price = graphene.Field(
        TaxedMoney, description="The price, with any discount subtracted."
    )
    price_undiscounted = graphene.Field(
        TaxedMoney, description="The price without any discount."
    )
    price_local_currency = graphene.Field(
        TaxedMoney, description="The discounted price in the local currency."
    )

    class Meta:
        description = "Represents availability of a variant in the storefront."


class ProductPricingInfo(BasePricingInfo):
    price_range = graphene.Field(
        TaxedMoneyRange,
        description="The discounted price range of the product variants.",
    )
    price_range_undiscounted = graphene.Field(
        TaxedMoneyRange,
        description="The undiscounted price range of the product variants.",
    )
    price_range_local_currency = graphene.Field(
        TaxedMoneyRange,
        description=(
            "The discounted price range of the product variants "
            "in the local currency."
        ),
    )

    class Meta:
        description = "Represents availability of a product in the storefront."


class ChannelContextType(DjangoObjectType):
    class Meta:
        abstract = True

    @staticmethod
    def resolver_with_context(
        attname, default_value, root: ChannelContext, info, **args
    ):
        resolver = get_default_resolver()
        return resolver(attname, default_value, root.node, info, **args)

    @staticmethod
    def resolve_id(root: ChannelContext, _info):
        return root.node.pk

    @classmethod
    def is_type_of(cls, root: ChannelContext, info):
        return super().is_type_of(root.node, info)


@key(fields="id")
class ProductVariant(ChannelContextType, CountableDjangoObjectType):
    quantity = graphene.Int(
        required=True,
        description="Quantity of a product available for sale.",
        deprecation_reason=(
            "Use the stock field instead. This field will be removed after 2020-07-31."
        ),
    )
    quantity_allocated = graphene.Int(
        required=False,
        description="Quantity allocated for orders.",
        deprecation_reason=(
            "Use the stock field instead. This field will be removed after 2020-07-31."
        ),
    )
    stock_quantity = graphene.Int(
        required=True,
        description="Quantity of a product available for sale.",
        deprecation_reason=(
            "Use the quantityAvailable field instead. "
            "This field will be removed after 2020-07-31."
        ),
    )
    price = graphene.Field(
        Money,
        description=(
            "Base price of a product variant. "
            "This field is restricted for admins. "
            "Use the pricing field to get the public price for customers."
        ),
    )
    pricing = graphene.Field(
        VariantPricingInfo,
        description=(
            "Lists the storefront variant's pricing, the current price and discounts, "
            "only meant for displaying."
        ),
    )
    is_available = graphene.Boolean(
        description="Whether the variant is in stock and visible or not.",
        deprecation_reason=(
            "Use the stock field instead. This field will be removed after 2020-07-31."
        ),
    )
    attributes = graphene.List(
        graphene.NonNull(SelectedAttribute),
        required=True,
        description="List of attributes assigned to this variant.",
    )
    cost_price = graphene.Field(Money, description="Cost price of the variant.")
    margin = graphene.Int(description="Gross margin percentage value.")
    quantity_ordered = graphene.Int(description="Total quantity ordered.")
    revenue = graphene.Field(
        TaxedMoney,
        period=graphene.Argument(ReportingPeriod),
        description=(
            "Total revenue generated by a variant in given period of time. Note: this "
            "field should be queried using `reportProductSales` query as it uses "
            "optimizations suitable for such calculations."
        ),
    )
    images = graphene.List(
        lambda: ProductImage, description="List of images for the product variant."
    )
    translation = TranslationField(
        ProductVariantTranslation, type_name="product variant"
    )
    digital_content = graphene.Field(
        DigitalContent, description="Digital content for the product variant."
    )
    stocks = graphene.Field(
        graphene.List(Stock),
        description="Stocks for the product variant.",
        country_code=graphene.Argument(
            CountryCodeEnum,
            description="Two-letter ISO 3166-1 country code.",
            required=False,
        ),
    )
    quantity_available = graphene.Int(
        required=True,
        description="Quantity of a product available for sale in one checkout.",
        country_code=graphene.Argument(
            CountryCodeEnum,
            description=(
                "Two-letter ISO 3166-1 country code. When provided, the exact quantity "
                "from a warehouse operating in shipping zones that contain this "
                "country will be returned. Otherwise, it will return the maximum "
                "quantity from all shipping zones."
            ),
        ),
    )

    class Meta:
        default_resolver = ChannelContextType.resolver_with_context
        description = (
            "Represents a version of a product such as different size or color."
        )
        only_fields = ["id", "name", "product", "sku", "track_inventory", "weight"]
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.ProductVariant

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_stocks(root: ChannelContext, info, country_code=None):
        if not country_code:
            return root.node.stocks.annotate_available_quantity()
        return root.node.stocks.for_country(country_code).annotate_available_quantity()

    @staticmethod
    def resolve_quantity_available(root: ChannelContext, info, country_code=None):
        if not root.node.track_inventory:
            return settings.MAX_CHECKOUT_LINE_QUANTITY

        return AvailableQuantityByProductVariantIdAndCountryCodeLoader(
            info.context
        ).load((root.node.id, country_code))

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_digital_content(root: ChannelContext, *_args):
        return getattr(root.node, "digital_content", None)

    @staticmethod
    def resolve_stock_quantity(root: ChannelContext, info):
        if not root.node.track_inventory:
            return settings.MAX_CHECKOUT_LINE_QUANTITY

        return AvailableQuantityByProductVariantIdAndCountryCodeLoader(
            info.context
        ).load((root.node.id, info.context.country))

    @staticmethod
    def resolve_attributes(root: ChannelContext, info):
        return SelectedAttributesByProductVariantIdLoader(info.context).load(
            root.node.id
        )

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_margin(root: ChannelContext, *_args):
        return get_margin_for_variant(root.node)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_cost_price(root: ChannelContext, *_args):
        return root.node.cost_price

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_price(root: ChannelContext, *_args):
        return root.node.price

    @staticmethod
    def resolve_pricing(root, info):
        if not root.channel_slug:
            return None

        context = info.context
        product = ProductByIdLoader(context).load(root.node.product_id)
        channel_listing = ProductChannelListingByProductIdAndChanneSlugLoader(
            context
        ).load((root.node.product_id, root.channel_slug))
        collections = CollectionsByProductIdLoader(context).load(root.node.product_id)

        def calculate_pricing_info(discounts):
            def calculate_pricing_with_product(product):
                def calculate_pricing_with_channel_listings(channel_listing):
                    def calculate_pricing_with_collections(collections):
                        availability = get_variant_availability(
                            variant=root.node,
                            product=product,
                            channel_listing=channel_listing,
                            collections=collections,
                            discounts=discounts,
                            country=context.country,
                            local_currency=context.currency,
                            plugins=context.plugins,
                        )
                        return VariantPricingInfo(**asdict(availability))

                    return collections.then(calculate_pricing_with_collections)

                return channel_listing.then(calculate_pricing_with_channel_listings)

            return product.then(calculate_pricing_with_product)

        return (
            DiscountsByDateTimeLoader(context)
            .load(info.context.request_time)
            .then(calculate_pricing_info)
        )

    @staticmethod
    def resolve_product(root: ChannelContext, info):
        return ProductByIdLoader(info.context).load(root.node.product_id)

    @staticmethod
    def resolve_is_available(root: ChannelContext, info):
        if not root.node.track_inventory:
            return True

        def is_variant_in_stock(available_quantity):
            return available_quantity > 0

        return (
            AvailableQuantityByProductVariantIdAndCountryCodeLoader(info.context)
            .load((root.node.id, info.context.country))
            .then(is_variant_in_stock)
        )

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_quantity(root: ChannelContext, info):
        return get_available_quantity(root.node, info.context.country)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_quantity_ordered(root: ChannelContext, *_args):
        # This field is added through annotation when using the
        # `resolve_report_product_sales` resolver.
        return getattr(root.node, "quantity_ordered", None)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_quantity_allocated(root: ChannelContext, info):
        country = info.context.country
        return get_quantity_allocated(root.node, country)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_revenue(root: ChannelContext, *_args, period):
        start_date = reporting_period_to_date(period)
        return calculate_revenue_for_variant(root.node, start_date)

    @staticmethod
    def resolve_images(root: ChannelContext, *_args):
        return root.node.images.all()

    # FIXME: Implement access to node at resolver level if possible
    # @classmethod
    # def get_node(cls, info, pk):
    #     user = info.context.user
    #     channel = get_default_channel_or_graphql_error()
    #     visible_products = models.Product.objects.visible_to_user(
    #         user, channel.slug
    #     ).values_list("pk", flat=True)
    #     qs = cls._meta.model.objects.filter(product__id__in=visible_products)
    #     return qs.filter(pk=pk).first()

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_private_meta(root: ChannelContext, _info):
        return resolve_private_meta(root.node, _info)

    @staticmethod
    def resolve_meta(root: ChannelContext, _info):
        return resolve_meta(root.node, _info)

    @staticmethod
    def __resolve_reference(root, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.node.id)

    @staticmethod
    def resolve_weight(root: ChannelContext, _info, **_kwargs):
        return convert_weight_to_default_weight_unit(root.node.weight)


@key(fields="id")
class Product(ChannelContextType, CountableDjangoObjectType):
    url = graphene.String(
        description="The storefront URL for the product.",
        required=True,
        deprecation_reason="This field will be removed after 2020-07-31.",
    )
    thumbnail = graphene.Field(
        Image,
        description="The main thumbnail for a product.",
        size=graphene.Argument(graphene.Int, description="Size of thumbnail."),
    )
    pricing = graphene.Field(
        ProductPricingInfo,
        description=(
            "Lists the storefront product's pricing, the current price and discounts, "
            "only meant for displaying."
        ),
    )
    is_available = graphene.Boolean(
        description="Whether the product is in stock and visible or not."
    )
    minimal_variant_price = graphene.Field(
        Money, description="The price of the cheapest variant (including discounts)."
    )
    tax_type = graphene.Field(
        TaxType, description="A type of tax. Assigned by enabled tax gateway"
    )
    attributes = graphene.List(
        graphene.NonNull(SelectedAttribute),
        required=True,
        description="List of attributes assigned to this product.",
    )
    channel_listing = graphene.List(
        graphene.NonNull(ProductChannelListing),
        description="List of availability in channels for the product.",
    )
    purchase_cost = graphene.Field(MoneyRange)
    margin = graphene.Field(Margin)
    image_by_id = graphene.Field(
        lambda: ProductImage,
        id=graphene.Argument(graphene.ID, description="ID of a product image."),
        description="Get a single product image by ID.",
    )
    variants = graphene.List(
        ProductVariant, description="List of variants for the product."
    )
    images = graphene.List(
        lambda: ProductImage, description="List of images for the product."
    )
    collections = graphene.List(
        lambda: Collection, description="List of collections for the product."
    )
    translation = TranslationField(ProductTranslation, type_name="product")

    class Meta:
        default_resolver = ChannelContextType.resolver_with_context
        description = "Represents an individual item for sale in the storefront."
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Product
        only_fields = [
            "category",
            "charge_taxes",
            "description",
            "description_json",
            "id",
            "name",
            "slug",
            "product_type",
            "seo_description",
            "seo_title",
            "updated_at",
            "weight",
        ]

    @staticmethod
    def resolve_category(root: ChannelContext, info):
        category_id = root.node.category_id
        if category_id is None:
            return None
        return CategoryByIdLoader(info.context).load(category_id)

    @staticmethod
    def resolve_tax_type(root: ChannelContext, info):
        tax_data = info.context.plugins.get_tax_code_from_object_meta(root.node)
        return TaxType(tax_code=tax_data.code, description=tax_data.description)

    @staticmethod
    def resolve_thumbnail(root: ChannelContext, info, *, size=255):
        def return_first_thumbnail(images):
            image = images[0] if images else None
            if image:
                url = get_product_image_thumbnail(image, size, method="thumbnail")
                alt = image.alt
                return Image(alt=alt, url=info.context.build_absolute_uri(url))
            return None

        return (
            ImagesByProductIdLoader(info.context)
            .load(root.node.id)
            .then(return_first_thumbnail)
        )

    @staticmethod
    def resolve_url(*_args):
        return ""

    @staticmethod
    def resolve_pricing(root: ChannelContext, info):
        if not root.channel_slug:
            return None

        context = info.context
        channel_listing = ProductChannelListingByProductIdAndChanneSlugLoader(
            context
        ).load((root.node.id, root.channel_slug))
        variants = ProductVariantsByProductIdLoader(context).load(root.node.id)
        collections = CollectionsByProductIdLoader(context).load(root.node.id)

        def calculate_pricing_info(discounts):
            def calculate_pricing_with_channel_listings(channel_listing):
                def calculate_pricing_with_variants(variants):
                    def calculate_pricing_with_collections(collections):
                        availability = get_product_availability(
                            product=root.node,
                            channel_listing=channel_listing,
                            variants=variants,
                            collections=collections,
                            discounts=discounts,
                            country=context.country,
                            local_currency=context.currency,
                            plugins=context.plugins,
                        )
                        return ProductPricingInfo(**asdict(availability))

                    return collections.then(calculate_pricing_with_collections)

                return variants.then(calculate_pricing_with_variants)

            return channel_listing.then(calculate_pricing_with_channel_listings)

        return (
            DiscountsByDateTimeLoader(context)
            .load(info.context.request_time)
            .then(calculate_pricing_info)
        )

    @staticmethod
    def resolve_is_available(root: ChannelContext, info):
        if not root.channel_slug:
            return None

        country = info.context.country
        in_stock = is_product_in_stock(root.node, country)
        is_visible = models.ProductChannelListing.objects.filter(
            product=root.node, channel__slug=root.channel_slug
        ).exists()
        return is_visible and in_stock

    @staticmethod
    def resolve_attributes(root: ChannelContext, info):
        return SelectedAttributesByProductIdLoader(info.context).load(root.node.id)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_purchase_cost(root: ChannelContext, *_args):
        purchase_cost, _ = get_product_costs_data(root.node)
        return purchase_cost

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_margin(root: ChannelContext, *_args):
        _, margin = get_product_costs_data(root.node)
        return Margin(margin[0], margin[1])

    @staticmethod
    def resolve_image_by_id(root: ChannelContext, info, id):
        pk = get_database_id(info, id, ProductImage)
        try:
            return root.node.images.get(pk=pk)
        except models.ProductImage.DoesNotExist:
            raise GraphQLError("Product image not found.")

    @staticmethod
    def resolve_images(root: ChannelContext, info, **_kwargs):
        return ImagesByProductIdLoader(info.context).load(root.node.id)

    @staticmethod
    def resolve_variants(root: ChannelContext, info, **_kwargs):
        return ProductVariantsByProductIdLoader(info.context).load(root.node.id)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_channel_listing(root: ChannelContext, info, **_kwargs):
        return ProductChannelListingByProductIdLoader(info.context).load(root.node.id)

    @staticmethod
    def resolve_collections(root: ChannelContext, *_args):
        return root.node.collections.all()

    # FIXME: Implement access to node at resolver level if possible
    # @classmethod
    # def get_node(cls, info, pk):
    #     if info.context:
    #         user = info.context.user
    #         channel = get_default_channel_or_graphql_error()
    #         qs = cls._meta.model.objects.visible_to_user(user, channel.slug)
    #         return qs.filter(pk=pk).first()
    #     return None

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_private_meta(root: ChannelContext, _info):
        return resolve_private_meta(root.node, _info)

    @staticmethod
    def resolve_meta(root: ChannelContext, _info):
        return resolve_meta(root.node, _info)

    @staticmethod
    def __resolve_reference(root: ChannelContext, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.node.id)

    @staticmethod
    def resolve_weight(root: ChannelContext, _info, **_kwargs):
        return convert_weight_to_default_weight_unit(root.node.weight)


@key(fields="id")
class ProductType(CountableDjangoObjectType):
    products = ChannelContextFilterConnectionField(
        Product,
        channel=graphene.String(
            description="Slug of a channel for which the data should be returned."
        ),
        description="List of products of this type.",
    )
    tax_rate = TaxRateType(description="A type of tax rate.")
    tax_type = graphene.Field(
        TaxType, description="A type of tax. Assigned by enabled tax gateway"
    )
    variant_attributes = graphene.List(
        Attribute, description="Variant attributes of that product type."
    )
    product_attributes = graphene.List(
        Attribute, description="Product attributes of that product type."
    )
    available_attributes = FilterInputConnectionField(
        Attribute, filter=AttributeFilterInput()
    )

    class Meta:
        description = (
            "Represents a type of product. It defines what attributes are available to "
            "products of this type."
        )
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.ProductType
        only_fields = [
            "has_variants",
            "id",
            "is_digital",
            "is_shipping_required",
            "name",
            "slug",
            "weight",
            "tax_type",
        ]

    @staticmethod
    def resolve_tax_type(root: models.ProductType, info):
        tax_data = info.context.plugins.get_tax_code_from_object_meta(root)
        return TaxType(tax_code=tax_data.code, description=tax_data.description)

    @staticmethod
    def resolve_tax_rate(root: models.ProductType, _info, **_kwargs):
        # FIXME this resolver should be dropped after we drop tax_rate from API
        if not hasattr(root, "meta"):
            return None
        return root.get_value_from_metadata("vatlayer.code")

    @staticmethod
    def resolve_product_attributes(root: models.ProductType, *_args, **_kwargs):
        return root.product_attributes.product_attributes_sorted().all()

    @staticmethod
    def resolve_variant_attributes(root: models.ProductType, *_args, **_kwargs):
        return root.variant_attributes.variant_attributes_sorted().all()

    @staticmethod
    def resolve_products(root: models.ProductType, info, channel=None, **_kwargs):
        user = info.context.user
        if channel is None:
            channel = get_default_channel_or_graphql_error().slug
        qs = root.products.visible_to_user(user, channel)
        return ChannelQsContext(qs=qs, channel_slug=channel)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_available_attributes(root: models.ProductType, info, **kwargs):
        qs = models.Attribute.objects.get_unassigned_attributes(root.pk)
        return resolve_attributes(info, qs=qs, **kwargs)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_private_meta(root: models.ProductType, _info):
        return resolve_private_meta(root, _info)

    @staticmethod
    def resolve_meta(root: models.ProductType, _info):
        return resolve_meta(root, _info)

    @staticmethod
    def __resolve_reference(root, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.id)

    @staticmethod
    def resolve_weight(root: models.ProductType, _info, **_kwargs):
        return convert_weight_to_default_weight_unit(root.weight)


@key(fields="id")
class Collection(CountableDjangoObjectType):
    products = ChannelContextFilterConnectionField(
        Product,
        channel=graphene.String(
            description="Slug of a channel for which the data should be returned."
        ),
        description="List of products in this collection.",
    )
    background_image = graphene.Field(
        Image, size=graphene.Int(description="Size of the image.")
    )
    translation = TranslationField(CollectionTranslation, type_name="collection")

    class Meta:
        description = "Represents a collection of products."
        only_fields = [
            "description",
            "description_json",
            "id",
            "is_published",
            "name",
            "publication_date",
            "seo_description",
            "seo_title",
            "slug",
        ]
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Collection

    @staticmethod
    def resolve_background_image(root: models.Collection, info, size=None, **_kwargs):
        if root.background_image:
            return Image.get_adjusted(
                image=root.background_image,
                alt=root.background_image_alt,
                size=size,
                rendition_key_set="background_images",
                info=info,
            )

    @staticmethod
    def resolve_products(root: models.Collection, info, channel=None, **kwargs):
        user = info.context.user
        if channel is None:
            channel = get_default_channel_or_graphql_error().slug
        qs = root.products.collection_sorted(user, channel)
        return ChannelQsContext(qs=qs, channel_slug=channel)

    @classmethod
    def get_node(cls, info, id):
        if info.context:
            user = info.context.user
            qs = cls._meta.model.objects.visible_to_user(user)
            return qs.filter(id=id).first()
        return None

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_private_meta(root: models.Collection, _info):
        return resolve_private_meta(root, _info)

    @staticmethod
    def resolve_meta(root: models.Collection, _info):
        return resolve_meta(root, _info)

    @staticmethod
    def __resolve_reference(root, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.id)


@key(fields="id")
class Category(CountableDjangoObjectType):
    ancestors = PrefetchingConnectionField(
        lambda: Category, description="List of ancestors of the category."
    )
    products = ChannelContextFilterConnectionField(
        Product,
        channel=graphene.String(
            description="Slug of a channel for which the data should be returned."
        ),
        description="List of products in the category.",
    )
    url = graphene.String(
        description="The storefront's URL for the category.",
        deprecation_reason="This field will be removed after 2020-07-31.",
    )
    children = PrefetchingConnectionField(
        lambda: Category, description="List of children of the category."
    )
    background_image = graphene.Field(
        Image, size=graphene.Int(description="Size of the image.")
    )
    translation = TranslationField(CategoryTranslation, type_name="category")

    class Meta:
        description = (
            "Represents a single category of products. Categories allow to organize "
            "products in a tree-hierarchies which can be used for navigation in the "
            "storefront."
        )
        only_fields = [
            "description",
            "description_json",
            "id",
            "level",
            "name",
            "parent",
            "seo_description",
            "seo_title",
            "slug",
        ]
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Category

    @staticmethod
    def resolve_ancestors(root: models.Category, info, **_kwargs):
        return root.get_ancestors()

    @staticmethod
    def resolve_background_image(root: models.Category, info, size=None, **_kwargs):
        if root.background_image:
            return Image.get_adjusted(
                image=root.background_image,
                alt=root.background_image_alt,
                size=size,
                rendition_key_set="background_images",
                info=info,
            )

    @staticmethod
    def resolve_children(root: models.Category, info, **_kwargs):
        return root.children.all()

    @staticmethod
    def resolve_url(root: models.Category, _info):
        return ""

    @staticmethod
    def resolve_products(root: models.Category, _info, channel=None, **_kwargs):
        tree = root.get_descendants(include_self=True)
        if channel is None:
            channel = get_default_channel_or_graphql_error().slug
        qs = models.Product.objects.published(channel)
        qs = qs.filter(category__in=tree).distinct()
        return ChannelQsContext(qs=qs, channel_slug=channel)

    @staticmethod
    @permission_required(ProductPermissions.MANAGE_PRODUCTS)
    def resolve_private_meta(root: models.Category, _info):
        return resolve_private_meta(root, _info)

    @staticmethod
    def resolve_meta(root: models.Category, _info):
        return resolve_meta(root, _info)

    @staticmethod
    def __resolve_reference(root, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.id)


@key(fields="id")
class ProductImage(CountableDjangoObjectType):
    url = graphene.String(
        required=True,
        description="The URL of the image.",
        size=graphene.Int(description="Size of the image."),
    )

    class Meta:
        description = "Represents a product image."
        only_fields = ["alt", "id", "sort_order"]
        interfaces = [relay.Node]
        model = models.ProductImage

    @staticmethod
    def resolve_url(root: models.ProductImage, info, *, size=None):
        if size:
            url = get_thumbnail(root.image, size, method="thumbnail")
        else:
            url = root.image.url
        return info.context.build_absolute_uri(url)

    @staticmethod
    def __resolve_reference(root, _info, **_kwargs):
        return graphene.Node.get_node_from_global_id(_info, root.id)
