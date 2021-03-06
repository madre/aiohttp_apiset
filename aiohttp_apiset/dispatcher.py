import asyncio
import keyword
import re
from urllib import parse

from aiohttp import hdrs
from aiohttp import web_urldispatcher as wu


class SubLocation:
    SPLIT = re.compile(r'/((?:(?:\{.+?\})|(?:[^/{}]+))+)')

    def __init__(self, name, formatter=None, parent=None):
        self._name = name
        self._formatter = formatter or name
        self._parent = parent
        self._subs = {}
        self._patterns = []
        self._routes = {}

    @property
    def name(self):
        return self._name

    def url(self, *, parts=None, query=None):
        formatters = [self._formatter]
        parent = self._parent
        while parent is not None:
            formatters.append(parent._formatter)
            parent = parent._parent
        url = '/'.join(reversed(formatters))
        if parts:
            url = url.format_map(parts)
        return self._append_query(url, query)

    _append_query = staticmethod(wu.Resource._append_query)

    def __repr__(self):
        return '<SubLocation {name}, url={url}>' \
               ''.format(name=self.name, url=self.url())

    @classmethod
    def split(cls, path):
        locations = [i for i in cls.SPLIT.split(path) if i]
        if locations[-1] == '/':
            locations[-1] = ''
        return locations

    def resolve(self, method: str, path: str, match_dict: dict):
        allowed_methods = set()

        if path is None:
            allowed_methods.update(self._routes)
            if method in self._routes:
                route = self._routes[method]
            elif hdrs.METH_ANY in self._routes:
                route = self._routes[hdrs.METH_ANY]
            else:
                return None, allowed_methods
            return wu.UrlMappingMatchInfo(match_dict, route), allowed_methods
        elif not path:
            location = path
            tail = None
        else:
            parts = path.split('/', 1)
            if len(parts) == 2:
                location, tail = parts
            else:
                location = parts[0]
                tail = None

        if location in self._subs:
            return self._subs[location].resolve(
                method=method,
                path=tail,
                match_dict=match_dict)

        for pattern, sublocation in self._patterns:
            m = pattern.match(path)
            if m is not None:
                for key, value in m.groupdict().items():
                    match_dict[key] = parse.unquote(value)

                index = m.end()
                if len(path) > index:
                    if path[index] == '/':
                        index += 1
                    tail = path[index:]
                else:
                    tail = None

                return sublocation.resolve(
                    method=method, path=tail, match_dict=match_dict)
        return None, allowed_methods

    def register_route(self, path: list, route: wu.AbstractRoute):
        if not path:
            assert route.method not in self._routes, self
            self._routes[route.method] = route
            return self

        location_name, *path = path

        if '{' in location_name:
            for pattern, loc in self._patterns:
                if loc.name == location_name:
                    location = loc
                    break
            else:
                pattern, formatter = \
                    TreeUrlDispatcher.get_pattern_formatter(location_name)
                location = SubLocation(location_name, formatter, parent=self)
                self._patterns.append((re.compile(pattern), location))
        elif location_name in self._subs:
            location = self._subs[location_name]
        else:
            location = SubLocation(location_name, parent=self)
            self._subs[location_name] = location

        return location.register_route(path, route)


class Route(wu.ResourceRoute):
    def __init__(self, method, handler, resource, *,
                 expect_handler=None, location=None):
        super().__init__(method, handler,
                         expect_handler=expect_handler,
                         resource=resource)
        self._location = location

    @property
    def location(self):
        return self._location

    @location.setter
    def location(self, value):
        self._location = value


class TreeResource(wu.Resource):
    route_class = Route
    sublocation_class = SubLocation

    def __init__(self, *, name=None):
        super().__init__(name=name)
        self._location = self.sublocation_class('')

    def add_route(self, method, handler, *,
                  path='/', expect_handler=None):
        path = self.sublocation_class.split(path)
        route = self.route_class(method, handler, self,
                                 expect_handler=expect_handler)
        location = self._location.register_route(path, route)
        route.location = location
        return route

    @asyncio.coroutine
    def resolve(self, method, path):
        return self._location.resolve(method, path[1:], {})

    def get_info(self):
        return {}

    def url(self, *, parts, query=None):
        return self._append_query('/', query)

    def __repr__(self):
        name = "'" + self.name + "' " if self.name is not None else ""
        return "<TreeResource {name}".format(name=name)


class BaseUrlDispatcher(wu.UrlDispatcher):

    def validate_name(self, name: str):
        """
        Fragment aiohttp.web_urldispatcher.UrlDispatcher#_reg_resource
        """
        parts = self.NAME_SPLIT_RE.split(name)
        for part in parts:
            if not part.isidentifier() or keyword.iskeyword(part):
                raise ValueError('Incorrect route name {!r}, '
                                 'the name should be a sequence of '
                                 'python identifiers separated '
                                 'by dash, dot or column'.format(name))
        if name in self._named_resources:
            raise ValueError('Duplicate {!r}, '
                             'already handled by {!r}'
                             .format(name, self._named_resources[name]))

    @classmethod
    def get_pattern_formatter(cls, location):
        """
        Fragment from aiohttp.web_urldispatcher.UrlDispatcher#add_resource
        :param location:
        :return:
        """
        pattern = ''
        formatter = ''
        for part in cls.ROUTE_RE.split(location):
            match = cls.DYN.match(part)
            if match:
                pattern += '(?P<{}>{})'.format(match.group('var'), cls.GOOD)
                formatter += '{' + match.group('var') + '}'
                continue

            match = cls.DYN_WITH_RE.match(part)
            if match:
                pattern += '(?P<{var}>{re})'.format(**match.groupdict())
                formatter += '{' + match.group('var') + '}'
                continue

            if '{' in part or '}' in part:
                raise ValueError("Invalid path '{}'['{}']".format(
                    location, part))

            formatter += part
            pattern += re.escape(part)

        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(
                "Bad pattern '{}': {}".format(pattern, exc)) from None
        return pattern, formatter


class TreeUrlDispatcher(BaseUrlDispatcher):
    tree_resource_class = TreeResource

    def __init__(self):
        super().__init__()
        assert not self._resources
        self._resources.append(self.tree_resource_class())

    @property
    def tree_resource(self) -> TreeResource:
        return self._resources[0]

    def add_route(self, method, path, handler,
                  *, name=None, expect_handler=None):
        if name:
            self.validate_name(name)

        route = self.tree_resource.add_route(
            method=method, handler=handler,
            path=path, expect_handler=expect_handler,
        )
        if name:
            self._named_resources[name] = route.location
        return route
