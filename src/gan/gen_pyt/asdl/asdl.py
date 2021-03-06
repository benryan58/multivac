# -*- coding: utf-8 -*-
from collections import OrderedDict
from itertools import chain


class ASDLGrammar(object):
    """
    Collection of types, constructors and productions
    """

    def __init__(self, productions):
        # productions are indexed by their head types
        self._productions = OrderedDict()
        self._constructor_production_map = dict()
        for prod in productions:
            if prod.type not in self._productions:
                self._productions[prod.type] = list()
            self._productions[prod.type].append(prod)
            self._constructor_production_map[prod.constructor.name] = prod

        self.root_type = productions[0].type
        # number of constructors
        self.size = sum(len(head) for head in self._productions.values())

        # get entities to their ids map
        self.prod2id = {prod: i for i, prod in enumerate(self.productions)}
        self.type2id = {type: i for i, type in enumerate(self.types)}
        self.field2id = {field: i for i, field in enumerate(self.fields)}

        self.id2prod = {i: prod for i, prod in enumerate(self.productions)}
        self.id2type = {i: type for i, type in enumerate(self.types)}
        self.id2field = {i: field for i, field in enumerate(self.fields)}

    def __len__(self):
        return self.size

    @property
    def productions(self):
        return sorted(chain.from_iterable(self._productions.values()), key=lambda x: repr(x))

    def __getitem__(self, datum):
        if isinstance(datum, str):
            return self._productions[ASDLType(datum)]
        elif isinstance(datum, ASDLType):
            return self._productions[datum]

    def get_prod_by_ctr_name(self, name):
        return self._constructor_production_map[name]

    @property
    def types(self):
        if not hasattr(self, '_types'):
            all_types = set()
            for prod in self.productions:
                all_types.add(prod.type)
                all_types.update(map(lambda x: x.type, prod.constructor.fields))

            self._types = sorted(all_types, key=lambda x: x.name)

        return self._types

    @property
    def fields(self):
        if not hasattr(self, '_fields'):
            all_fields = set()
            for prod in self.productions:
                all_fields.update(prod.constructor.fields)

            self._fields = sorted(all_fields, key=lambda x: (x.name, x.type.name, x.cardinality))

        return self._fields

    @property
    def primitive_types(self):
        return filter(lambda x: isinstance(x, ASDLPrimitiveType), self.types)

    @property
    def composite_types(self):
        return filter(lambda x: isinstance(x, ASDLCompositeType), self.types)

    def is_composite_type(self, asdl_type):
        return asdl_type in self.composite_types

    def is_primitive_type(self, asdl_type):
        return asdl_type in self.primitive_types


class ASDLProduction(object):
    def __init__(self, type, constructor):
        self.type = type
        self.constructor = constructor

    @property
    def fields(self):
        return self.constructor.fields

    def __getitem__(self, field_name):
        return self.constructor[field_name]

    def __hash__(self):
        h = hash(self.type) ^ hash(self.constructor)

        return h

    def __eq__(self, other):
        return isinstance(other, ASDLProduction) and \
               self.type == other.type and \
               self.constructor == other.constructor

    def __lt__(self, other):
        return other.__repr__() < self.__repr__()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return self.constructor.name


class ASDLConstructor(object):
    def __init__(self, name, fields=None):
        self.name = name
        self.fields = []
        if fields:
            self.fields = list(fields)

    def __getitem__(self, field_name):
        for field in self.fields:
            if field.name == field_name:
                return field

        raise KeyError

    def __hash__(self):
        h = hash(self.name)
        for field in self.fields:
            h ^= hash(field)

        return h

    def __eq__(self, other):
        return isinstance(other, ASDLConstructor) and \
               self.name == other.name and \
               self.fields == other.fields

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self, plain=False):
        plain_repr = '%s -> (%s)' % (self.name, ', '.join(f.__repr__(plain=True) for f in self.fields))
        if plain:
            return plain_repr
        else:
            return 'Constructor(%s)' % plain_repr


class Field(object):

    def __init__(self, name, type, cardinality):
        self.name = name
        self.type = type

        assert cardinality in ['single', 'optional', 'multiple']
        self.cardinality = cardinality

    def __hash__(self):
        h = hash(self.name) ^ hash(self.type)
        h ^= hash(self.cardinality)

        return h

    def __eq__(self, other):
        return isinstance(other, Field) and \
               self.name == other.name and \
               self.type == other.type and \
               self.cardinality == other.cardinality

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self, plain=False):
        plain_repr = '%s%s' % (self.type.__repr__(plain=True),
                               Field.get_cardinality_repr(self.cardinality))
        if plain:
            return plain_repr
        else:
            return 'Field(%s)' % plain_repr

    @staticmethod
    def get_cardinality_repr(cardinality):
        return '' if cardinality == 'single' else '?' if cardinality == 'optional' else '*'


class ASDLType(object):

    def __init__(self, type_name):
        self.name = type_name

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, ASDLType) and self.name == other.name

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self, plain=False):
        plain_repr = self.name
        if plain:
            return plain_repr
        else:
            return '%s(%s)' % (self.__class__.__name__, plain_repr)


class ASDLCompositeType(ASDLType):
    pass


class ASDLPrimitiveType(ASDLType):
    pass
