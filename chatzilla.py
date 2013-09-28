from gevent import monkey
from flask import Flask, Response, render_template, request
from socketio import socketio_manage
from socketio.namespace import BaseNamespace
from socketio.mixins import BroadcastMixin, RoomsMixin
from time import time
import os
import pymongo

monkey.patch_all()

application = Flask(__name__)
application.debug = True
application.config['PORT'] = 5000


class ChatNamespace(BaseNamespace, BroadcastMixin, RoomsMixin):
    
    stats = {
        "people" : []
    }

    @classmethod
    def get_db_conn(cls):
        if getattr(cls, 'db', None):
            return cls.db

        # Retrieve database credentials from environment
        db_uri = os.getenv('DATABASE_URI', 'mongodb://localhost:27017/')
        db_user = os.getenv('DATABASE_USER', '')
        db_password = os.getenv('DATABASE_PASSWORD', '')
        db_name = os.getenv('DATABASE_NAME', '')

        # Initialize database client and database connection
        try:
            db_client = pymongo.MongoReplicaSetClient(db_uri, replicaSet='repl0')
        except:
            db_client = pymongo.MongoClient(db_uri)
        db = db_client[db_name]
        if db_user and db_password:
            db.authenticate(db_user, db_password)

        cls.db = db

        return cls.db

    def initialize(self):
        self.logger = application.logger
        self.log("Socketio session started")

    def log(self, message):
        self.logger.info("[{0}] {1}".format(self.socket.sessid, message))

    def report_stats(self):
        self.broadcast_event("stats",self.stats)

    def recv_connect(self):
        self.log("New connection")

    def recv_disconnect(self):
        self.log("Client disconnected")
        
        if self.session.has_key("email"):
            email = self.session['email']

            self.broadcast_event_not_me("debug", "%s left" % email)
            
            self.stats["people"] = filter(lambda e : e != email, self.stats["people"])
            self.report_stats()

    def on_join(self, email):
        self.log("%s joined chat" % email)
        self.session['email'] = email

        if not email in self.stats["people"]:
            self.stats["people"].append(email) 

        self.report_stats()

        return True, email

    def on_subscribe(self, room):
        self.join(room)

        # Retrieve message history for this room
        messages = self.get_message_history(room)

        return True, messages

    def on_unsubscribe(self, room):
        self.leave(room)
        return True, room

    def on_message(self, message):
        room = message['room']
        message_content = message['content']

        message_data = {
            "sender" : self.session["email"],
            "room" : room,
            "content" : message_content,
            "sent" : time()*1000 #ms
        }
        self.emit_to_room(room, "message", message_data)

        # Record message to database
        self.record_message(message_data)

        return True, message_data

    def record_message(self, message_data):
        # Create a copy since the insert command will mutate the dict
        message_data_copy = message_data.copy()

        # Insert into database
        # Set write concern to 0 because I don't care about immediate
        # consistency
        db = self.__class__.get_db_conn()
        db['chat_messages'].insert(message_data_copy, w=0)

    def get_message_history(self, room):
        db = self.__class__.get_db_conn()

        # Use a list comprehension to return a list instead of a cursor
        messages = [m for m in
                    db['chat_messages'].find(
                        {'room' : room},
                        {'_id' : 0}
                    ).sort('sent', pymongo.ASCENDING)
                   ]

        return messages


@application.route('/', methods=['GET'])
def landing():
    return render_template('landing.html')

@application.route('/socket.io/<path:remaining>')
def socketio(remaining):
    try:
        socketio_manage(request.environ, {'/chat': ChatNamespace}, request)
    except:
        application.logger.error("Exception while handling socketio connection",
                         exc_info=True)
    return Response()
