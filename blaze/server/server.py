from __future__ import absolute_import, division, print_function

import collections
from datetime import datetime
import errno
import functools
from hashlib import md5
import os
import re
import socket
from warnings import warn

from datashape import discover, pprint
import flask
from flask import Blueprint, Flask, request, Response
from flask.ext.cors import cross_origin
from toolz import valmap

import blaze
from blaze import compute, resource
from blaze.compatibility import ExitStack
from blaze.compute import compute_up
from blaze.expr import utils as expr_utils

from .serialization import json, all_formats
from ..interactive import InteractiveSymbol
from ..expr import Expr, symbol


__all__ = 'Server', 'to_tree', 'from_tree', 'expr_md5'

# http://www.speedguide.net/port.php?port=6363
# http://en.wikipedia.org/wiki/List_of_TCP_and_UDP_port_numbers
DEFAULT_PORT = 6363


api = Blueprint('api', __name__)
pickle_extension_api = Blueprint('pickle_extension_api', __name__)


_no_default = object()  # sentinel


def _get_option(option, options, default=_no_default):
    try:
        return options[option]
    except KeyError:
        if default is not _no_default:
            return default

        # Provides a more informative error message.
        raise TypeError(
            'The blaze api must be registered with {option}'.format(
                option=option,
            ),
        )


def ensure_dir(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def _register_api(app, options, first_registration=False):
    """
    Register the data with the blueprint.
    """
    _get_data.cache[app] = _get_option('data', options)
    _get_format.cache[app] = dict(
        (f.name, f) for f in _get_option('formats', options)
    )
    _get_auth.cache[app] = (
        _get_option('authorization', options, None) or (lambda a: True)
    )
    allow_profiler = _get_option('allow_profiler', options, False)
    profiler_output = _get_option('profiler_output', options, None)
    profile_by_default = _get_option('profile_by_default', options, False)
    if not allow_profiler and (profiler_output or profile_by_default):
        raise ValueError(
            "cannot set %s%s%s when 'allow_profiler' is False" % (
                'profiler_output' if profiler_output else '',
                ' or ' if profiler_output and profile_by_default else '',
                'profile_by_default' if profile_by_default else '',
            ),
        )
    if allow_profiler:
        if profiler_output is None:
            profiler_output = 'profiler_output'
        if profiler_output != ':response':
            ensure_dir(profiler_output)

    _get_profiler_info.cache[app] = (
        allow_profiler, profiler_output, profile_by_default
    )

    # Call the original register function.
    Blueprint.register(api, app, options, first_registration)

api.register = _register_api


def per_app_accesor(name):
    def _get():
        return _get.cache[flask.current_app]
    _get.cache = {}
    _get.__name__ = '_get' + name
    return _get


def _get_format(name):
    return _get_format.cache[flask.current_app][name]
_get_format.cache = {}

_get_data = per_app_accesor('data')
_get_auth = per_app_accesor('auth')
_get_profiler_info = per_app_accesor('profiler_info')


def expr_md5(expr):
    """Returns the md5 hash of the str of the expression.

    Parameters
    ----------
    expr : Expr
        The expression to hash.

    Returns
    -------
    hexdigest : str
        The hexdigest of the md5 of the str of ``expr``.
    """
    exprstr = str(expr)
    if not isinstance(exprstr, bytes):
        exprstr = exprstr.encode('utf-8')
    return md5(exprstr).hexdigest()


def _prof_path(profiler_output, expr):
    """Get the path to write the data for a profile run of ``expr``.

    Parameters
    ----------
    profiler_output : str
        The director to write into.
    expr : Expr
        The expression that was run.

    Returns
    -------
    prof_path : str
        The filepath to write the new profiler data.

    Notes
    -----
    This function ensures that the dirname of the returned path exists.
    """
    dir_ = os.path.join(
        profiler_output,
        expr_md5(expr),  # use the md5 so the client knows where to look
    )
    ensure_dir(dir_)
    return os.path.join(dir_, str(int(datetime.utcnow().timestamp())))


def authorization(f):
    @functools.wraps(f)
    def authorized(*args, **kwargs):
        if not _get_auth()(request.authorization):
            return Response(
                'bad auth token',
                401,
                {'WWW-Authenticate': 'Basic realm="Login Required"'},
            )

        return f(*args, **kwargs)
    return authorized


def check_request(f):
    @functools.wraps(f)
    def check():
        content_type = request.headers['content-type']
        matched = mimetype_regex.match(content_type)

        if matched is None:
            return 'Unsupported serialization format %s' % content_type, 415

        try:
            serial = _get_format(matched.groups()[0])
        except KeyError:
            return (
                "Unsupported serialization format '%s'" % matched.groups()[0],
                415,
            )

        try:
            payload = serial.loads(request.data)
        except ValueError:
            return ("Bad data.  Got %s " % request.data, 400)  # 400: Bad Request

        return f(payload, serial)
    return check


class Server(object):

    """ Blaze Data Server

    Host local data through a web API

    Parameters
    ----------
    data : dict, optional
        A dictionary mapping dataset name to any data format that blaze
        understands.
    formats : iterable, optional
        An iterable of supported serialization formats. By default, the
        server will support JSON.
        A serialization format is an object that supports:
        name, loads, and dumps.
    authorization : callable, optional
        A callable to be used to check the auth header from the client.
        This callable should accept a single argument that will either be
        None indicating that no header was passed, or an object
        containing a username and password attribute. By default, all requests
        are allowed.
    allow_profiler : bool, optional
        Allow payloads to specify `"profile": true` which will run the
        computation under cProfile.
    profiler_output : str, optional
        The directory to write pstats files after profile runs.
        The files will be written in a structure like:

          {profiler_output}/{hash(expr)}/{timestamp}

        This defaults to a relative path of `profiler_output`.
        This requires `allow_profiler=True`.

        If this is the string ':response' then writing to the local filesystem
        is disabled. Only requests that specify `profiler_output=':response'`
        will be served. All others will return a 403.
    profile_by_default : bool, optional
        Run the profiler on any computation that does not explicitly set
        "profile": false.
        This requires `allow_profiler=True`.

    Examples
    --------
    >>> from pandas import DataFrame
    >>> df = DataFrame([[1, 'Alice',   100],
    ...                 [2, 'Bob',    -200],
    ...                 [3, 'Alice',   300],
    ...                 [4, 'Dennis',  400],
    ...                 [5,  'Bob',   -500]],
    ...                columns=['id', 'name', 'amount'])

    >>> server = Server({'accounts': df})
    >>> server.run() # doctest: +SKIP
    """
    def __init__(self,
                 data=None,
                 formats=None,
                 authorization=None,
                 allow_profiler=False,
                 profiler_output=None,
                 profile_by_default=False):
        app = self.app = Flask('blaze.server.server')
        if data is None:
            data = dict()
        app.register_blueprint(
            api,
            data=data,
            formats=formats if formats is not None else (json,),
            authorization=authorization,
            allow_profiler=allow_profiler,
            profiler_output=profiler_output,
            profile_by_default=profile_by_default,
        )
        self.data = data

    def run(self, port=DEFAULT_PORT, retry=False, **kwargs):
        """Run the server.

        Parameters
        ----------
        port : int, optional
            The port to bind to.
        retry : bool, optional
            If the port is busy, should we retry with the next available port?
        **kwargs
            Forwarded to the underlying flask app's ``run`` method.

        Notes
        -----
        This function blocks forever when successful.
        """
        self.port = port
        try:
            # Blocks until the server is shut down.
            self.app.run(port=port, **kwargs)
        except socket.error:
            if not retry:
                raise

            warn("Oops, couldn't connect on port %d.  Is it busy?" % port)
            # Attempt to start the server on a new port.
            self.run(port=port + 1, retry=retry, **kwargs)


@api.route('/datashape', methods=['GET'])
@cross_origin(origins='*', methods=['GET'])
@authorization
def shape():
    return pprint(discover(_get_data()), width=0)


def to_tree(expr, names=None):
    """ Represent Blaze expression with core data structures

    Transform a Blaze expression into a form using only strings, dicts, lists
    and base types (int, float, datetime, ....)  This form can be useful for
    serialization.

    Parameters
    ----------
    expr : Expr
        A Blaze expression

    Examples
    --------

    >>> t = symbol('t', 'var * {x: int32, y: int32}')
    >>> to_tree(t) # doctest: +SKIP
    {'op': 'Symbol',
     'args': ['t', 'var * { x : int32, y : int32 }', False]}


    >>> to_tree(t.x.sum()) # doctest: +SKIP
    {'op': 'sum',
     'args': [
         {'op': 'Column',
         'args': [
             {
              'op': 'Symbol'
              'args': ['t', 'var * { x : int32, y : int32 }', False]
             }
             'x']
         }]
     }

    Simplify expresion using explicit ``names`` dictionary.  In the example
    below we replace the ``Symbol`` node with the string ``'t'``.

    >>> tree = to_tree(t.x, names={t: 't'})
    >>> tree # doctest: +SKIP
    {'op': 'Column', 'args': ['t', 'x']}

    >>> from_tree(tree, namespace={'t': t})
    t.x

    See Also
    --------

    from_tree
    """
    if names and expr in names:
        return names[expr]
    if isinstance(expr, tuple):
        return [to_tree(arg, names=names) for arg in expr]
    if isinstance(expr, expr_utils._slice):
        return to_tree(expr.as_slice(), names=names)
    if isinstance(expr, slice):
        return {'op': 'slice',
                'args': [to_tree(arg, names=names) for arg in
                         [expr.start, expr.stop, expr.step]]}
    elif isinstance(expr, InteractiveSymbol):
        return to_tree(symbol(expr._name, expr.dshape), names)
    elif isinstance(expr, Expr):
        return {'op': type(expr).__name__,
                'args': [to_tree(arg, names) for arg in expr._args]}
    else:
        return expr


def expression_from_name(name):
    """

    >>> expression_from_name('By')
    <class 'blaze.expr.split_apply_combine.By'>

    >>> expression_from_name('And')
    <class 'blaze.expr.arithmetic.And'>
    """
    import blaze
    if hasattr(blaze, name):
        return getattr(blaze, name)
    if hasattr(blaze.expr, name):
        return getattr(blaze.expr, name)
    for signature, func in compute_up.funcs.items():
        try:
            if signature[0].__name__ == name:
                return signature[0]
        except TypeError:
            pass
    raise ValueError('%s not found in compute_up' % name)


def from_tree(expr, namespace=None):
    """ Convert core data structures to Blaze expression

    Core data structure representations created by ``to_tree`` are converted
    back into Blaze expressions.

    Parameters
    ----------
    expr : dict

    Examples
    --------

    >>> t = symbol('t', 'var * {x: int32, y: int32}')
    >>> tree = to_tree(t)
    >>> tree # doctest: +SKIP
    {'op': 'Symbol',
     'args': ['t', 'var * { x : int32, y : int32 }', False]}

    >>> from_tree(tree)
    t

    >>> tree = to_tree(t.x.sum())
    >>> tree # doctest: +SKIP
    {'op': 'sum',
     'args': [
         {'op': 'Field',
         'args': [
             {
              'op': 'Symbol'
              'args': ['t', 'var * { x : int32, y : int32 }', False]
             }
             'x']
         }]
     }

    >>> from_tree(tree)
    sum(t.x)

    Simplify expresion using explicit ``names`` dictionary.  In the example
    below we replace the ``Symbol`` node with the string ``'t'``.

    >>> tree = to_tree(t.x, names={t: 't'})
    >>> tree # doctest: +SKIP
    {'op': 'Field', 'args': ['t', 'x']}

    >>> from_tree(tree, namespace={'t': t})
    t.x

    See Also
    --------

    to_tree
    """
    if isinstance(expr, dict):
        op, args = expr['op'], expr['args']
        if 'slice' == op:
            return expr_utils._slice(*[from_tree(arg, namespace)
                                       for arg in args])
        if hasattr(blaze.expr, op):
            cls = getattr(blaze.expr, op)
        else:
            cls = expression_from_name(op)
        if 'Symbol' in op:
            children = [from_tree(arg) for arg in args]
        else:
            children = [from_tree(arg, namespace) for arg in args]
        return cls(*children)
    elif isinstance(expr, (list, tuple)):
        return tuple(from_tree(arg, namespace) for arg in expr)
    if namespace and expr in namespace:
        return namespace[expr]
    else:
        return expr


mimetype_regex = re.compile(r'^application/vnd\.blaze\+(%s)$' %
                            '|'.join(x.name for x in all_formats))


@api.route('/compute', methods=['POST', 'HEAD', 'OPTIONS'])
@cross_origin(origins='*', methods=['POST', 'HEAD', 'OPTIONS'])
@authorization
@check_request
def compserver(payload, serial):
    (allow_profiler,
     default_profiler_output,
     profile_by_default) = _get_profiler_info()
    requested_profiler_output = payload.get(
        'profiler_output',
        default_profiler_output,
    )
    profile = payload.get('profile')
    profiling = (
        allow_profiler and
        (profile or (profile_by_default and requested_profiler_output))
    )
    if profile and not allow_profiler:
        return (
            'profiling is disabled on this server',
            403,
        )

    with ExitStack() as response_construction_context_stack:
        if profiling:
            from cProfile import Profile

            if (default_profiler_output == ':response' and
                requested_profiler_output != ':response'):
                # writing to the local filesystem is disabled
                return (
                    "local filepaths are disabled on this server, only"
                    " ':response' is allowed for the 'profiler_output' field",
                    403,
                )

            profiler_output = requested_profiler_output
            profiler = Profile()
            profiler.enable()
            # ensure that we stop profiling in the case of an exception
            response_construction_context_stack.callback(profiler.disable)

        ns = payload.get('namespace', {})
        compute_kwargs = payload.get('compute_kwargs') or {}
        odo_kwargs = payload.get('odo_kwargs') or {}
        dataset = _get_data()
        ns[':leaf'] = symbol('leaf', discover(dataset))

        expr = from_tree(payload['expr'], namespace=ns)
        assert len(expr._leaves()) == 1
        leaf = expr._leaves()[0]

        try:
            result = serial.materialize(
                compute(expr, {leaf: dataset}, **compute_kwargs),
                expr.dshape,
                odo_kwargs,
            )
        except NotImplementedError as e:
            # 501: Not Implemented
            return ("Computation not supported:\n%s" % e, 501)
        except Exception as e:
            # 500: Internal Server Error
            return (
                "Computation failed with message:\n%s: %s" % (
                    type(e).__name__,
                    e
                ),
                500,
            )

        response = {
            'datashape': pprint(expr.dshape, width=0),
            'data': serial.data_dumps(result),
            'names': expr.fields
        }

    if profiling:
        import marshal
        from pstats import Stats

        if profiler_output == ':response':
            from pandas.compat import BytesIO
            file = BytesIO()
        else:
            file = open(_prof_path(profiler_output, expr), 'wb')

        with file:
            # Use marshal to dump the stats data to the given file.
            # This is taken from cProfile which unfortunately does not have
            # an api that allows us to pass the file object directly, only
            # a file path.
            marshal.dump(Stats(profiler).stats, file)
            if profiler_output == ':response':
                response['profiler_output'] = {'__!bytes': file.getvalue()}

    return serial.dumps(response)


@api.route('/add', methods=['POST', 'HEAD', 'OPTIONS'])
@cross_origin(origins='*', methods=['POST', 'HEAD', 'OPTIONS'])
@authorization
@check_request
def addserver(payload, serial):
    """Add a data resource to the server.

    The reuest should contain serialized MutableMapping (dictionary) like
    object, and the server should already be hosting a MutableMapping
    resource.
    """
    data = _get_data.cache[flask.current_app]

    data_not_mm_msg = ("Cannot update blaze server data since its current data"
                       " is a %s and not a mutable mapping (dictionary like).")
    if not isinstance(data, collections.MutableMapping):
        return (data_not_mm_msg % type(data), 422)

    payload_not_mm_msg = ("Cannot update blaze server with a %s payload, since"
                          " it is not a mutable mapping (dictionary like).")
    if not isinstance(payload, collections.MutableMapping):
        return (payload_not_mm_msg % type(payload), 422)

    data.update(valmap(resource, payload))
    return 'OK'
