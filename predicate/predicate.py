import itertools
import re

import django
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Manager
from django.db.models import QuerySet
from django.db.models.constants import LOOKUP_SEP
from django.db.models.query_utils import Q


QUERY_TERMS = set([
    'exact', 'iexact', 'contains', 'icontains', 'gt', 'gte', 'lt', 'lte', 'in',
    'startswith', 'istartswith', 'endswith', 'iendswith', 'range', 'year',
    'month', 'day', 'week_day', 'isnull', 'search', 'regex', 'iregex',
])


def eval_wrapper(children):
    """
    generator to yield child nodes, or to wrap filter expressions
    """
    lookups = LookupNode()
    for child in children:
        if isinstance(child, P):
            yield child
        elif isinstance(child, tuple):
            lookup, value = child
            lookups[lookup] = value
        else:
            raise ValueError(child)
    yield lookups


class P(Q):
    """
    A Django 'predicate' construct

    This is a variation on Q objects, but instead of being used to generate
    SQL, they are used to test a model instance against a set of conditions.
    """

    # allow the use of the 'in' operator for membership testing
    def __contains__(self, obj):
        # TODO: This overrides Q's __contains__ method. It should only have
        # the custom behavior for non-Node objects.
        return self.eval(obj)

    def eval(self, instance):
        """
        Returns true if the model instance matches this predicate
        """
        evaluators = {"AND": all, "OR": any}
        evaluator = evaluators[self.connector]
        ret = evaluator(c.eval(instance) for c in eval_wrapper(self.children))
        if self.negated:
            return not ret
        else:
            return ret


class LookupEvaluator(object):
    """
    A thin wrapper around a filter expression tuple of (lookup-type, value) to
    provide an eval method
    """

    def __init__(self, expr):
        self.lookup, self.value = expr

    # Comparison functions

    def _exact(self, values):
        return self.value in values

    def _iexact(self, values):
        expected = self.value.lower()
        return any(value is not None and expected == value.lower()
                   for value in values)

    def _contains(self, values):
        return any(value is not None and self.value in value
                   for value in values)

    def _icontains(self, values):
        expected = self.value.lower()
        return any(value is not None and expected in value
                   for value in values)

    def _gt(self, values):
        return any(value is not None and value > self.value
                   for value in values)

    def _gte(self, values):
        return any(value is not None and value >= self.value
                   for value in values)

    def _lt(self, values):
        return any(value is not None and value < self.value
                   for value in values)

    def _lte(self, values):
        return any(value is not None and value <= self.value
                   for value in values)

    def _startswith(self, values):
        return any(value is not None and value.startswith(self.value)
                   for value in values)

    def _istartswith(self, values):
        expected_value = self.value.lower()
        return any(
            value is not None and value.lower().startswith(expected_value)
            for value in values)

    def _endswith(self, values):
        return any(
            value is not None and value.lower().endswith(self.value)
            for value in values)

    def _iendswith(self, values):
        expected_value = self.value.lower()
        return any(
            value is not None and value.lower().endswith(expected_value)
            for value in values)

    def _in(self, values):
        return bool(set(values) & set(self.value))

    def _range(self, values):
        return any(value is not None and self.value[0] < value < self.value[1]
                   for value in values)

    def _year(self, values):
        return any(value is not None and value.year == self.value
                   for value in values)

    def _month(self, values):
        return any(value is not None and value.month == self.value
                   for value in values)

    def _day(self, values):
        return any(value is not None and value.day == self.value
                   for value in values)

    def _week_day(self, values):
        # Counterintuitively, the __week_day lookup does not use the .weekday()
        # python method, but instead some custom django weekday thing
        # (Sunday=1 to Saturday=7). This is equivalent to:
        # (isoweekday mod 7) + 1.
        # https://docs.python.org/2/library/datetime.html#datetime.date.isoweekday
        #
        # See docs at https://docs.djangoproject.com/en/dev/ref/models/querysets/#week-day
        # and https://code.djangoproject.com/ticket/10345 for additional
        # discussion.
        return any(
            value is not None
            and (value.isoweekday() % 7) + 1 == self.value
            for value in values)

    def _isnull(self, values):
        if self.value:
            return None in values
        else:
            return None not in values

    def _search(self, values):
        return self._contains(values)

    def _regex(self, values):
        """
        Note that for queries - this can be DB specific syntax
        here we just use Python
        """
        regex = re.compile(self.value)
        return any(
            value is not None and regex.search(value)
            for value in values)

    def _iregex(self, values):
        regex = re.compile(self.value, flags=re.I)
        return any(
            value is not None and regex.search(value)
            for value in values)


class LookupComponent(str):
    def __repr__(self):
        return '{self.__class__.__name__}({repr})'.format(
            self=self,
            repr=super(LookupComponent, self).__repr__())

    @classmethod
    def parse(cls, lookup):
        """
        Parses a lookup__string into a list of LookupComponent objects.
        """
        if not lookup:  # Handle '' standing in for leaf components in lookups.
            return []
        return map(cls, lookup.split(LOOKUP_SEP))

    @property
    def is_query(self):
        """
        Returns true if a query lookup like __in or __gte.

        TODO: Expand this to handle custom registered lookups.
        """
        return self in QUERY_TERMS

    def values_list(self, obj):
        if obj is None:
            return [None]
        elif self == LookupComponent.EMPTY:
            return [obj]
        values = []
        if django.VERSION < (1, 8):
            field, model, direct,  m2m = obj._meta.get_field_by_name(self)
        else:
            field = obj._meta.get_field(self)
            direct = not field.auto_created or field.concrete
        accessor = self if direct else field.get_accessor_name()
        try:
            result = getattr(obj, accessor)
        except ObjectDoesNotExist:
            values.append(None)
        else:
            if isinstance(result, (QuerySet, Manager)):
                values.extend(result.all())
            else:
                values.append(result)
        return values


LookupComponent.EMPTY = LookupComponent('')
UNDEFINED = object()
GET = object()


class LookupNode(object):
    def __init__(self, lookups=None):
        lookups = lookups or {}
        self.children = {}
        if lookups is not None:
            for lookup, value in lookups.viewitems():
                self[lookup] = value

    @property
    def value(self):
        return self.children.get(LookupComponent.EMPTY, UNDEFINED)

    @value.setter
    def value(self, value):
        self.children[LookupComponent.EMPTY] = value

    def __setitem__(self, lookup, value):
        components = LookupComponent.parse(lookup)
        cur = self
        for component in components:
            prev = cur
            cur = cur.children.get(component)
            if cur is None:
                prev.children[component] = cur = LookupNode()
        cur.value = value

    def __getitem__(self, lookup):
        components = LookupComponent.parse(lookup)
        cur = self
        for component in components:
            cur = cur.children[component]
        return cur

    def iteritems(self, lookup_stack=None):
        lookup_stack = [] if lookup_stack is None else lookup_stack
        for component, node in self.children.viewitems():
            if component == LookupComponent.EMPTY:
                yield (LOOKUP_SEP.join(lookup_stack), self.value)
            else:
                lookup_stack.append(component)
                for item in node.iteritems(lookup_stack=lookup_stack):
                    yield item
                lookup_stack.pop()

    def to_dict(self):
        return dict(self.iteritems())

    def __repr__(self):
        return 'LookupNode(lookups=%r)' % self.to_dict()

    def eval(self, instance):
        query_values_lookups = self.convert_to_query_values_node()
        values = query_values_lookups.values(instance)
        for node in values:
            node_matches = True
            for lookup, value in node.iteritems():
                queries = self[lookup]
                for query, query_value in queries.iteritems():
                    if query == LookupComponent.EMPTY:
                        # No query lookup was specified, so assume __exact
                        query = 'exact'
                    evaluator = LookupEvaluator((query, query_value))
                    comparison_func = getattr(evaluator, '_' + query)
                    if not comparison_func([value]):
                        node_matches = False
                        break
                if not node_matches:
                    break
            if node_matches:
                return True
        return False

    def convert_to_query_values_node(self):
        """
        Returns a version of self that has had all query lookup components
        replaced by GET operations.

        Used for evaluating predicates.
        """
        lookups = LookupNode()
        for lookup, _ in self.iteritems():
            parsed = LookupComponent.parse(lookup)
            if parsed[-1].is_query:
                parsed.pop()
            lookups[LOOKUP_SEP.join(parsed)] = GET
        return lookups

    def values(self, obj):
        """
        Returns a list of LookupNode instances matching the GET lookups in self.
        """
        lookup2values = {}
        for lookup, child in self.children.items():
            lookup_objects = lookup.values_list(obj)
            if lookup == LookupComponent.EMPTY:
                child_values = lookup_objects
            else:
                child_values = []
                for lookup_obj in lookup_objects:
                    child_values.extend(child.values(lookup_obj))
            lookup2values[lookup] = child_values

        # Construct a cartesian product of all returned values. This
        # corresponds to a database join among the lookups.
        # TODO: Does this handle inner and outer joins properly?
        children_iters = (
            itertools.izip(itertools.repeat(lookup), values)
            for lookup, values in lookup2values.items())
        results = []
        for child_product in itertools.product(*children_iters):
            node = LookupNode()
            for lookup, value in child_product:
                node.children[lookup] = value
            results.append(node)
        return results
