.. _swh-web-client:

.. include:: README.rst

.. _swh-web-client-auth:

Authentication
--------------

If you have a user account registered on `Software Heritage Identity Provider`_,
it is possible to authenticate requests made to the Web APIs through the use of
OpenID Connect bearer tokens. Sending authenticated requests can notably
allow to lift API rate limiting depending on your permissions.

To get these tokens, a dedicated CLI tool is made available when installing
``swh-web-client``:

.. code-block:: text

  $ swh auth
  Usage: swh auth [OPTIONS] COMMAND [ARGS]...

    Authenticate Software Heritage users with OpenID Connect.

    This CLI tool eases the retrieval of bearer tokens to authenticate a user
    querying the Software Heritage Web API.

  Options:
    --oidc-server-url TEXT  URL of OpenID Connect server (default to
                            "https://auth.softwareheritage.org/auth/")
    --realm-name TEXT       Name of the OpenID Connect authentication realm
                            (default to "SoftwareHeritage")
    --client-id TEXT        OpenID Connect client identifier in the realm
                            (default to "swh-web")
    -h, --help              Show this message and exit.

  Commands:
    login    Login and create new offline OpenID Connect session.
    logout   Logout from an offline OpenID Connect session.
    refresh  Refresh an offline OpenID Connect session.

In order to get your tokens, you need to use the ``login`` subcommand of
that CLI tool by passing your username as argument. You will be prompted
for your password and if the authentication succeeds a new OpenID Connect
session will be created and tokens will be dumped in JSON format to standard
output.

.. code-block:: text

  $ swh auth login <username>
  Password:
  {
      "access_token": ".......",
      "expires_in": 600,
      "refresh_expires_in": 0,
      "refresh_token": ".......",
      "token_type": "bearer",
      "id_token": ".......",
      "not-before-policy": 1584551170,
      "session_state": "c14e1b7b-8263-4852-bd1c-adc7bc12a136",
      "scope": "openid email profile offline_access"
  }

To authenticate yourself, you need to send the ``access_token`` value in
request headers when querying the Web APIs.
Considering you have stored the ``access_token`` value in a TOKEN environment
variable, you can perform an authenticated call the following way using ``curl``:

.. code-block:: text

  $ curl -H "Authorization: Bearer ${TOKEN}" http://localhost:5004/api/1/<endpoint>

The access token has a short living period (usually ten minutes) and must be
renewed on a regular basis by passing the ``refresh_token`` value as argument
of the ``refresh`` subcommand of the CLI tool. The new access token will be
dumped in JSON format to standard output. Note that the refresh token has a
much longer living period (usually several dozens of days) so you can use
it anytime while it is valid to get an access token without having to login
again.

.. code-block:: text

  $ swh auth refresh $REFRESH_TOKEN
  "......."

Note that if you intend to use the :class:`swh.web.client.client.WebAPIClient`
class, the access token renewal will be automatically handled if you call
method :meth:`swh.web.client.client.WebAPIClient.authenticate` prior to
sending any requests. To activate authentication, use the following code snippet::

  from swh.web.client import WebAPIClient

  REFRESH_TOKEN = '.......'  # Use "swh auth login" command to get it

  client = WebAPIClient()
  client.authenticate(REFRESH_TOKEN)

  # All requests to the Web API will be authenticated
  resp = client.get('swh:1:rev:aafb16d69fd30ff58afdd69036a26047f3aebdc6')

It is also possible to ``logout`` from the authenticated OpenID Connect session
which invalidates all previously emitted tokens.


.. code-block:: text

  $ swh auth logout $REFRESH_TOKEN
  Successfully logged out from OpenID Connect session

API Reference
-------------

.. toctree::
   :maxdepth: 2

   /apidoc/swh.web.client

.. _Software Heritage Identity Provider:
  https://auth.softwareheritage.org/auth/realms/SoftwareHeritage/account/